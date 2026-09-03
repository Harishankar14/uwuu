# LLVM Pass Transformation Analyzer


a) It records the state of a program's code before and after every single optimization pass.


b) Turns the whole history of changes into one unified explainable view.


FACTORS WHICH ARE DEPENDENT ON THE SYSTEM.

a) Attributed : So every change is tied to a specific pass, on a specific function,this would  be coming from the compiler as each pass runs.(Not determined by random guesses)


b) Quantified : Every change would be coming with the real numbers. This would mean (How many instructions were added, or how many were removed,how many vector operations appeared, how much time did it take (with optimization                    and without optimization).


c) Correlated : This would capture and answer the `why` question !! (Not an ML model), but pure compiler diagnostics !! (Compiler explains us as it should !! )


d) Comparison :  You can take two builds of a program (`lz4_base.c and lz4_edit.c`) and diff them (just like git !! ) . This analyzer will tell you what the optimizer did differently from the base version. 
