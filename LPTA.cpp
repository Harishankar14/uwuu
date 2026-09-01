#include "llvm/ADT/Any.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/Analysis/LazyCallGraph.h"
#include "llvm/Analysis/LoopInfo.h"
#include "llvm/IR/BasicBlock.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/Instruction.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/PassInstrumentation.h"
#include "llvm/IR/PassManager.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Plugins/PassPlugin.h"        // relocated in this tree
#include "llvm/Support/raw_ostream.h"
#include "llvm/Support/Format.h"
#include "llvm/Transforms/Utils/Cloning.h" // cloning function
#include "llvm/IR/ValueMap.h"  // ValueToValue
#include "llvm/IR/Constants.h"
#include "llvm/IR/DiagnosticHandler.h"
#include "llvm/IR/DiagnosticInfo.h"


#include <cstdint>
#include <optional>
#include <vector>
#include <algorithm>
#include <map>
#include <memory>
#include <string>
#include <chrono>
#include <functional>
#include <set>

using namespace llvm;

namespace {

static inline uint64_t mix(uint64_t H, uint64_t V){
  H ^= V + 0x9e3779b97f4a7c15ULL + (H << 6) + (H >> 2);
  return H;
}

  


// Same helper LLVM uses in StandardInstrumentations.cpp: any_cast on the
// address of the Any gives a pointer-to-pointer, so dereference once.
template <typename IRUnitT> const IRUnitT *unwrapIR(Any IR) {
  const IRUnitT **P = llvm::any_cast<const IRUnitT *>(&IR);
  return P ? *P : nullptr;
}

static std::unique_ptr<Module> cloneIntoOwnModule(const Function &F) {
  Module *SrcM = const_cast<Module *>(F.getParent());
  if (!SrcM) return nullptr;

  //cloning into the same func
  Function *Clone = Function::Create(F.getFunctionType(), F.getLinkage(),F.getAddressSpace(),F.getName() + ".lpta.clone", SrcM);
  ValueToValueMapTy VMap;
  auto DestArg = Clone->arg_begin();
  for (const Argument &A : F.args()) {
    DestArg->setName(A.getName());
    VMap[&A] = &*DestArg++;
  }
  SmallVector<ReturnInst *, 8> Returns;
  CloneFunctionInto(Clone, &F, VMap,CloneFunctionChangeType::LocalChangesOnly, Returns);
  // creating a seprate module for Clone so that diffEngine won't complain
  auto DstM = std::make_unique<Module>("lpta.snapshot", F.getContext());
  DstM->setDataLayout(SrcM->getDataLayout());
  Clone->removeFromParent();         // detach from SrcM (no longer pollutes live IR)
  DstM->getFunctionList().push_back(Clone);  // re-home into our module
  return DstM;
}


/*
  Visiting every instruction in whatever the IR unit ranon 
  We are not measuring unit types (SHOULD ASK why)*/

template <typename FnT> static bool forEachInst(Any IR, FnT Visit) {
  if (const auto *M = unwrapIR<Module>(IR)) {
    for (const Function &F : *M)
      for (const BasicBlock &BB : F)
        for (const Instruction &I : BB) Visit(I);
    return true;
  }
  if (const auto *F = unwrapIR<Function>(IR)) {
    for (const BasicBlock &BB : *F)
      for (const Instruction &I : BB) Visit(I);
    return true;
  }
  if (const auto *C = unwrapIR<LazyCallGraph::SCC>(IR)) {
    for (const LazyCallGraph::Node &N : *C)
      for (const BasicBlock &BB : N.getFunction())
        for (const Instruction &I : BB) Visit(I);
    return true;
  }
  if (const auto *L = unwrapIR<Loop>(IR)) {
    for (const BasicBlock *BB : L->blocks())
      for (const Instruction &I : *BB) Visit(I);
    return true;
  }
  return false;
}
/*
Inorder to avoid Renumbering withing, we use something called a structal Fingerprint 

per instructtion opcode + result tyype + operand count + operand type in program order

*/

struct  Fingerprint{
  uint64_t Count = 0;
  uint64_t Hash = 0;
  std::map<unsigned,uint64_t> Opcodes; // occurences of operation codde
  uint64_t vectorinsts=0;
};

static std::optional<Fingerprint> fingerprint(Any IR) {
  Fingerprint FP;
  bool Measured = forEachInst(IR, [&](const Instruction &I) {
    ++FP.Count;
    ++FP.Opcodes[I.getOpcode()];
    if (I.getType()->isVectorTy()) ++FP.vectorinsts;
    FP.Hash = mix(FP.Hash, I.getOpcode());
    FP.Hash = mix(FP.Hash, (unsigned)I.getType()->getTypeID());
    unsigned N = I.getNumOperands();
    FP.Hash = mix(FP.Hash, N);
    for (unsigned k = 0; k < N; ++k)
      FP.Hash = mix(FP.Hash, (unsigned)I.getOperand(k)->getType()->getTypeID());
  });
  if (!Measured) return std::nullopt;
  return FP;
}

static bool isScaffolding(StringRef PassID) {
  StringRef Name = PassID;
  size_t Lt = Name.find('<');
  if (Lt != StringRef::npos) Name = Name.substr(0, Lt);
  static const StringRef Special[] = {
      "PassManager", "PassAdaptor", "AnalysisManagerProxy",
      "DevirtSCCRepeatedPass", "ModuleInlinerWrapperPass"};
  for (StringRef S : Special)
    if (Name.ends_with(S)) return true;
  return false;
}

static const char *unitType(Any IR){
  if(unwrapIR<Module>(IR)) return "Module";
  if (unwrapIR<Function>(IR)) return "function";
  if (unwrapIR<LazyCallGraph::SCC>(IR)) return "scc";
  if (unwrapIR<Loop>(IR)) return "loop";
  return "unknown";
}

static std::string unitName(Any IR) {
  if (unwrapIR<Module>(IR)) return "(Module)";
  if (const auto *F = unwrapIR<Function>(IR)) return F->getName().str();
  if (const auto *C = unwrapIR<LazyCallGraph::SCC>(IR)) return C->getName();
  if (const auto *L = unwrapIR<Loop>(IR)) {
    if (const BasicBlock *H = L->getHeader()) return H->getName().str();
    return "(loop)";
  }
  return "(unknown)";
}
static LLVMContext *contextOf(Any IR) {
  if (const auto *M = unwrapIR<Module>(IR)) return &M->getContext();
  if (const auto *F = unwrapIR<Function>(IR)) return &F->getContext();
  if (const auto *L = unwrapIR<Loop>(IR))
    if (const BasicBlock *H = L->getHeader()) return &H->getContext();
  return nullptr;   // Module/Function cover the probe; SCC/Loop optional
}

static void writeJSONString(raw_ostream &OS, StringRef S) {
  OS << '"';
  for (char c : S) {
    switch (c) {
    case '"': OS << "\\\""; break;
    case '\\': OS << "\\\\"; break;
    case '\n': OS << "\\n"; break;
    case '\r': OS << "\\r"; break;
    case '\t': OS << "\\t"; break;
    default: OS << c;
    }
  }
  OS << '"';
}
//SCORING BLOCK 
enum class Cat { Memory, Control, Vector, Call, Arith, Cast, Other };
struct LPTARemarkHandler : public DiagnosticHandler {
  std::function<void(StringRef, StringRef, bool)> Sink;
  explicit LPTARemarkHandler(std::function<void(StringRef, StringRef, bool)> S)
      : Sink(std::move(S)) {}

  bool isAnalysisRemarkEnabled(StringRef) const override { return true; }
  bool isMissedOptRemarkEnabled(StringRef) const override { return true; }
  bool isPassedOptRemarkEnabled(StringRef) const override { return true; }
  bool isAnyRemarkEnabled() const override { return true; }

  bool handleDiagnostics(const DiagnosticInfo &DI) override {
    if (const auto *R = dyn_cast<DiagnosticInfoOptimizationBase>(&DI)) {
      if (Sink) Sink(R->getPassName(), R->getMsg(), isa<OptimizationRemarkMissed>(DI));
      return true;
    }
    return false;   // errors/warnings still print normally
  }
};

static Cat categorize(unsigned Op) {
  switch (Op) {
  case Instruction::Load:  case Instruction::Store: case Instruction::Alloca:
  case Instruction::GetElementPtr: case Instruction::Fence:
  case Instruction::AtomicCmpXchg: case Instruction::AtomicRMW:
    return Cat::Memory;
  case Instruction::ExtractElement: case Instruction::InsertElement:
  case Instruction::ShuffleVector:
    return Cat::Vector;
  case Instruction::Call: case Instruction::Invoke: case Instruction::CallBr:
    return Cat::Call;
  case Instruction::PHI:
    return Cat::Other; // control-flow bookkeeping — deliberately near-zero
  default: break;
  }
  if (Instruction::isTerminator(Op)) return Cat::Control;
  if (Instruction::isBinaryOp(Op) || Instruction::isUnaryOp(Op)) return Cat::Arith;
  if (Instruction::isCast(Op)) return Cat::Cast;
  return Cat::Other;
}

static double weight(Cat C) {
  switch (C) {
  case Cat::Vector:  return 4.0; // SIMD creation (LoopVectorize, SLP)
  case Cat::Memory:  return 3.0; // load/store elimination (SROA, GVN, DSE)
  case Cat::Call:    return 3.0; // inlining / devirtualization
  case Cat::Control: return 1.0; // branch folding (SimplifyCFG)
  case Cat::Arith:   return 1.0;
  case Cat::Cast:    return 0.5;
  case Cat::Other:   return 0.2; // PHI, freeze — kills LCSSA/LoopSimplify churn
  }
  return 0.2;
}

static double signifance(const std::map<unsigned,int64_t> &Delta,int64_t VectorDelta){
  double S = 0.0;
  for(auto &KV:Delta){
    int64_t A = KV.second < 0 ? -KV.second : KV.second;
    S += (double)A * weight(categorize(KV.first));
  }
  int64_t V = VectorDelta < 0 ? -VectorDelta : VectorDelta;
  S += (double)V * 2.0;
  return S;
}
struct Remark{
  std::string Pass,Msg;
  bool missed;
};

struct Pending {
  std::string Pass, UnitType, Unit;
  uint64_t Count, Hash;
  std::map<unsigned, uint64_t> Opcodes;
  uint64_t vectorinsts;
  std::unique_ptr<Module> Snapshot;          
  std::multiset<std::string> LoopKeys;       
  bool isLoop = false;                        
  std::chrono::steady_clock::time_point Start;
  std::vector<Remark> Remarks;
};
struct Record{
  std::string Pass, UnitType,Unit;
  uint64_t Before = 0;
  uint64_t After = 0;

  int64_t Delta = 0;
  bool Invalidated = false;
  std::map<unsigned,int64_t>OpcodeDelta;
  int64_t VectorDelta = 0;
  double Score = 0.0;
  double Relscore= 0.0;  
  std::vector<std::string>Removed;
  std::vector<std::string>Added;
  double TimeMs = 0.0;
  std::vector<Remark> Remarks;
};

// diff thing 
//class CaptureConsumer:public DifferenceEngine::Consumer{
// A Consumer that CAPTURES added/removed instructions as IR text, instead of
// printing them like llvm-diff's own DiffConsumer does.
// Build a medium-detail structural key for one instruction:
// opcode + result type + operand (type, and constant value if constant).
// Deliberately excludes SSA value NAMES so compiler renumbering never registers
// as a change — but includes constant values so x+1 vs x+2 is distinguishable.
static std::string instKey(const Instruction &I) {
  std::string S;
  raw_string_ostream OS(S);
  OS << I.getOpcodeName() << ':';
  I.getType()->print(OS);
  OS << '(';
  for (const Use &U : I.operands()) {
    const Value *V = U.get();
    V->getType()->print(OS);
    if (const auto *CI = dyn_cast<ConstantInt>(V))
      OS << "=i" << CI->getValue();
    else if (const auto *CF = dyn_cast<ConstantFP>(V)) {
      SmallString<16> Buf;
      CF->getValueAPF().toString(Buf);
      OS << "=f" << Buf;
    } else if (isa<Constant>(V) && V->hasName())
      OS << "=@" << V->getName();   // named global/func constant
    OS << ';';
  }
  OS << ')';
  return OS.str();
}
// loop instruction key capture 
// walk a loop's blocks, collect the structural key of each instruction
static std::multiset<std::string> LoopKeys(const Loop &L) {
  std::multiset<std::string> keys;
  for (const BasicBlock *BB : L.blocks())
    for (const Instruction &I : *BB)
      keys.insert(instKey(I));
  return keys;
}

// Compare two functions by multiset of instruction keys.
// Fills `removed` (in Before, not After) and `added` (in After, not Before)
// with the ACTUAL IR text of the differing instructions.
static void diffByKeys(const Function &Before, const Function &After,std::vector<std::string> &Removed,std::vector<std::string> &Added) {
  // key -> list of instruction pointers, per side (multiset via vector)
  std::map<std::string, std::vector<const Instruction *>> B, A;
  for (const BasicBlock &BB : Before)
    for (const Instruction &I : BB) B[instKey(I)].push_back(&I);
  for (const BasicBlock &BB : After)
    for (const Instruction &I : BB) A[instKey(I)].push_back(&I);

  auto toText = [](const Instruction *I) {
    std::string S; raw_string_ostream OS(S);
    I->print(OS); return OS.str();
  };

  // Removed: keys where Before has more occurrences than After.
  for (auto &KV : B) {
    size_t inA = A.count(KV.first) ? A[KV.first].size() : 0;
    for (size_t i = inA; i < KV.second.size(); ++i)
      Removed.push_back(toText(KV.second[i]));
  }
  // Added: keys where After has more occurrences than Before.
  for (auto &KV : A) {
    size_t inB = B.count(KV.first) ? B[KV.first].size() : 0;
    for (size_t i = inB; i < KV.second.size(); ++i)
      Added.push_back(toText(KV.second[i]));
  }

}
struct Recorder {
  std::vector<Pending> Stack;
  std::vector<Record> Records;
  int64_t Depth = 0;
  uint64_t PassesRun = 0;
  uint64_t AfterCalls = 0;
  std::unique_ptr<raw_fd_ostream> Out;
  bool Opened = false;
  bool HandlerInstalled = false;
  void installHandler(Any IR) {
    if (HandlerInstalled) return;
    if (LLVMContext *Ctx = contextOf(IR)) {
      Ctx->setDiagnosticHandler(std::make_unique<LPTARemarkHandler>(
          [this](StringRef Pass, StringRef Msg, bool Missed) {
            attachRemark(Pass, Msg, Missed);
          }));
      HandlerInstalled = true;
    }
  }
  void attachRemark(StringRef Pass, StringRef Msg, bool Missed) {
    if (Stack.empty()) return;               // remark outside a recorded pass — drop
    Stack.back().Remarks.push_back({Pass.str(), Msg.rtrim().str(), Missed});
  }


  void onBefore(StringRef P, Any IR) {
    if (isScaffolding(P)) return;
    auto FP = fingerprint(IR);
    if (!FP) return;

    std::unique_ptr<Module> Snap;
    std::multiset<std::string> LKeys;
    bool isLoopLocal = false;

    if(const Function *F = unwrapIR<Function>(IR))
      Snap = cloneIntoOwnModule(*F);
    else if (const Loop *L = unwrapIR<Loop>(IR)){
      LKeys = LoopKeys(*L);
      isLoopLocal = true;
    }
    Stack.push_back({P.str(), unitType(IR), unitName(IR), FP->Count, FP->Hash,FP->Opcodes,FP->vectorinsts,std::move(Snap),std::move(LKeys),isLoopLocal, std::chrono::steady_clock::now()});
    ++Depth;
  }
  void onAfter(StringRef P, Any IR) {
    installHandler(IR);
    if (isScaffolding(P)) return;
    if (Stack.empty()) return;
    if (Stack.back().Pass != P) return;
    auto EndTime = std::chrono::steady_clock::now();
    --Depth;

    Pending B = std::move(Stack.back());
    Stack.pop_back();
    ++PassesRun;
    double TimeMs = std::chrono::duration<double, std::milli>(EndTime - B.Start).count();  
    auto FP = fingerprint(IR);
    if (!FP) return;

    int64_t Delta = (int64_t)FP->Count - (int64_t)B.Count;
    bool Changed = (FP->Hash != B.Hash) || (Delta != 0);
    //if (!Changed) return;                 // only record passes that moved the IR
    if (!Changed && B.Remarks.empty()) return;   // a remark is a reason to record
    // opcode delta
    std::map<unsigned, int64_t> OD;
    for (auto &KV : FP->Opcodes) OD[KV.first] += (int64_t)KV.second;
    for (auto &KV : B.Opcodes)   OD[KV.first] -= (int64_t)KV.second;
    for (auto It = OD.begin(); It != OD.end();) {
      if (It->second == 0) It = OD.erase(It);
      else ++It;
    }

    int64_t VecDelta = (int64_t)FP->vectorinsts - (int64_t)B.vectorinsts;
    double Score = signifance(OD, VecDelta);
    uint64_t Denom = std::max<uint64_t>(std::max(B.Count, FP->Count), 1);
    double Relscore = Score / double(Denom);

    // structural diff — only for recorded function passes
    std::vector<std::string> Added, Removed;
    if (B.Snapshot) {
      const Function *After = unwrapIR<Function>(IR);
      Function *Before = &*B.Snapshot->begin();
      if (After)
        diffByKeys(*Before, *After, Removed, Added);
    }
    if (B.isLoop) {
      const Loop *After = unwrapIR<Loop>(IR);
      if (After) {
        std::multiset<std::string> now = LoopKeys(*After);
        std::multiset<std::string> before = B.LoopKeys;
        for (const std::string &k : before) {
          auto it = now.find(k);
          if (it != now.end()) now.erase(it);      // matched, consume
          else Removed.push_back(k);                // only in before
        }
        for (const std::string &k : now) Added.push_back(k);  // leftover in after
      } else {
        for (const std::string &k : B.LoopKeys) Removed.push_back(k);
      }
    }
    Record R{B.Pass, B.UnitType, B.Unit, B.Count, FP->Count, Delta,false, OD, VecDelta, Score, Relscore};
    R.Removed = std::move(Removed);       
    R.Added   = std::move(Added); 
    R.Remarks = std::move(B.Remarks); 
    R.TimeMs  = TimeMs;      
    emit(R);
    Records.push_back(R);
  }

  void onInvalidated(StringRef P) {
    if (isScaffolding(P)) return;
    if(Stack.empty()){
      return;
    }
    if (Stack.back().Pass != P){
      return;
    }
    --Depth;
    Pending B = std::move(Stack.back());
    Stack.pop_back();
    ++PassesRun;
    Record R{B.Pass, B.UnitType, B.Unit, B.Count, 0, 0, true, {}, 0, 0.0, 0.0};
    if(B.isLoop && !B.LoopKeys.empty())
      for(const std::string &k:B.LoopKeys)
        R.Removed.push_back(k);
    emit(R);
    Records.push_back(R);
  }
    // durable output: open once, write+flush per record ----
  raw_fd_ostream *out() {
    if (!Opened) {
      Opened = true;
      std::error_code EC;
      Out = std::make_unique<raw_fd_ostream>("lpta.json", EC);
      if (EC) { errs() << "LPTA: cannot open lpta.json: " << EC.message() << "\n"; Out.reset(); }
    }
    return Out.get();
  }

  void emit(const Record &R) {
    raw_fd_ostream *OS = out();
    if (!OS) return;
    *OS << "{\"pass\": ";        writeJSONString(*OS, R.Pass);
    *OS << ", \"unit_type\": ";  writeJSONString(*OS, R.UnitType);
    *OS << ", \"unit\": ";       writeJSONString(*OS, R.Unit);
    *OS << ", \"before\": " << R.Before;
    if (R.Invalidated)
      *OS << ", \"after\": null, \"delta\": null, \"invalidated\": true";
    else
      *OS << ", \"after\": " << R.After << ", \"delta\": " << R.Delta << ", \"invalidated\": false";
    *OS << ", \"score\": " << format("%.2f", R.Score);
    *OS << ", \"rel_score\": " << format("%.3f", R.Relscore);
    *OS << ", \"vector_delta\": " << R.VectorDelta;
    *OS << ", \"time_ms\": " << format("%.3f", R.TimeMs);
    std::map<std::string,int64_t>Byname;
    for (auto &KV : R.OpcodeDelta)
      Byname[Instruction::getOpcodeName(KV.first)] += KV.second;
    *OS << ", \"opcodes\": {";
    bool First = true;
    for (auto &KV : Byname) {
      if (KV.second == 0) continue;
      if (!First) *OS << ", ";
      First = false;
      writeJSONString(*OS, KV.first);
      *OS << ": " << KV.second;
    }
    *OS << "}";
    *OS << ", \"removed\": [";
    for (size_t i = 0; i < R.Removed.size(); ++i) {
      if (i) *OS << ", ";
      writeJSONString(*OS, R.Removed[i]);
    }
    *OS << "], \"added\": [";
    for (size_t i = 0; i < R.Added.size(); ++i) {
      if (i) *OS << ", ";
      writeJSONString(*OS, R.Added[i]);
    }
    *OS << "]";
    *OS << ", \"remarks\": [";
    for (size_t i = 0; i < R.Remarks.size(); ++i) {
      if (i) *OS << ", ";
      *OS << "{\"pass\": ";     writeJSONString(*OS, R.Remarks[i].Pass);
      *OS << ", \"missed\": " << (R.Remarks[i].missed ? "true" : "false");
      *OS << ", \"msg\": ";     writeJSONString(*OS, R.Remarks[i].Msg);
      *OS << "}";
    }
    *OS << "]";
    *OS << "}\n";
    OS->flush();
  }

  ~Recorder() {
    if (Depth != 0)
      errs() << "LPTA-PAIR: !!FINAL DEPTH=" << Depth << " (stack desynced — records after the first desync are suspect)\n";
    if (Out) Out->flush();
    std::vector<const Record *> Ranked;
    for (const Record &R : Records) Ranked.push_back(&R);
    std::sort(Ranked.begin(), Ranked.end(),[](const Record *A, const Record *B) { return A->Score > B->Score; });
    errs() << "LPTA: top passes by significance --\n";
    for (size_t i = 0; i < Ranked.size() && i < 8; ++i)
      errs() << format("  %7.2f  (rel %5.3f)  ", Ranked[i]->Score, Ranked[i]->Relscore) << Ranked[i]->Pass << " on " << Ranked[i]->Unit << "\n";
    errs() << "LPTA: recorded " << Records.size() << " IR-changing pass runs of " << PassesRun << " total; wrote lpta.json\n";
  }
};

static Recorder Rec;

} // namespace

extern "C" LLVM_ATTRIBUTE_WEAK ::llvm::PassPluginLibraryInfo
llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "LPTA", LLVM_VERSION_STRING,
          [](PassBuilder &PB) {
            auto *PIC = PB.getPassInstrumentationCallbacks();
            if (!PIC) return;
            PIC->registerBeforeNonSkippedPassCallback(
                [](StringRef P, Any IR) { Rec.onBefore(P, IR); });
            PIC->registerAfterPassCallback(
                [](StringRef P, Any IR, const PreservedAnalyses &) {
                  Rec.onAfter(P, IR);
                });
            PIC->registerAfterPassInvalidatedCallback(
                [](StringRef P, const PreservedAnalyses &) {
                  Rec.onInvalidated(P);
                });
          }};
}
