# LLVM Pass Transformation Analyzer


a) It records the state of a program's code before and after every single optimization pass.


b) Turns the whole history of changes into one unified explainable view.


FACTORS WHICH ARE DEPENDENT ON THE SYSTEM.

a) Attributed : So every change is tied to a specific pass, on a specific function,this would  be coming from the compiler as each pass runs.(Not determined by random guesses)


b) Quantified : Every change would be coming with the real numbers. This would mean (How many instructions were added, or how many were removed,how many vector operations appeared, how much time did it take (with optimization                    and without optimization).


c) Correlated : This would capture and answer the `why` question !! (Not an ML model), but pure compiler diagnostics !! (Compiler explains us as it should !! )


d) Comparison :  You can take two builds of a program (`lz4_base.c and lz4_edit.c`) and diff them (just like git !! ) . This analyzer will tell you what the optimizer did differently from the base version. 



## BUILD INSTRUCTIONS 

### Prerequisites

- **LLVM + Clang 18** (matching versions) with development headers
- **CMake** ≥ 3.13 and a C++17 compiler
- **Python 3** (for the CLI and web viewer — standard library only, no pip installs)
- Standard binutils (`objdump`, `size`) — used to measure the final machine code

```sh
sudo apt-get update
sudo apt-get install -y llvm-18 llvm-18-dev clang-18 cmake build-essential python3
```
Check the tools are visible:

```sh
clang-18 --version
llvm-config-18 --version    # should print 18.x
```

### 1. Clone

```sh
git clone https://github.com/<your-username>/LPTA.git
cd LPTA
```

### 2. Build the plugin

The core is a C++ plugin (`LPTA.cpp`) that loads into LLVM's optimizer. Build it with CMake:

```sh
mkdir -p build
cd build
cmake .. -DLLVM_DIR=$(llvm-config-18 --cmakedir)
cmake --build .
cd ..
```

This produces the plugin at `build/libLPTA.so`. Confirm it exists:

```sh
ls -la build/libLPTA.so
```

### 3. Run on a source file

`lpta-run.sh` drives the whole capture. It takes **LLVM IR** (`.ll`), so first emit unoptimized IR from your C/C++ source, then capture the pipeline:

```sh
# emit unoptimized IR (the optimizer runs on top of this)
clang -O2 -Xclang -disable-llvm-passes -emit-llvm -S lpta_test.c -o lpta_test.ll


# capture the -O2 pipeline -> writes example.json
./lpta-run.sh lpta_test.ll lpta_test -O 2
```

`lpta-run.sh` usage:

```
./lpta-run.sh <input.ll> <label> [-O <level>] [-x "<extra opt flags>"]
```

If your plugin isn't at the default `./build/libLPTA.so`, point to it:

```sh
LPTA_PLUGIN=/path/to/libLPTA.so ./lpta-run.sh example.ll example -O 2
```

### 4. View the results

**Command line:**

```sh
python3 lpta.py list lpta_test.json              # ranked overview of every pass
python3 lpta.py show lpta_test.json InstCombine  # one pass as a before/after diff
python3 lpta.py diff base.json edit.json       # compare two runs (regression view)
```

**Web viewer:**

```sh
python3 lpta_view.py lpta_test.json             # single run
python3 lpta_view.py base.json edit.json       # cross-run comparison
```

The viewer prints a local URL — open it in your browser.

### Quick end-to-end check

```sh
clang-18 -O0 -Xclang -disable-O0-optnone -emit-llvm -S example.c -o example.ll
./lpta-run.sh example.ll example -O 2
python3 lpta.py list example.json
```

If the last command prints a ranked table of passes and a codegen terminus, your build is working.
