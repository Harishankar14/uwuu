#include <stdint.h>

//Inlining, Instruction Combining (Strength Reduction)
static inline int multiply_by_eight(int val) {
    return val * 8; 
}

int process_data(int *arr, int len) {
    int sum = 0;
    int config_flag = 1; //Constant Propagation

    //SimplifyCFG, Dead Code Elimination (DCE)
    if (config_flag == 0) {
        return -1; // This entire branch should disappear
    }

    //Loop Unrolling, Loop Vectorization (LV), LICM
    for (int i = 0; i < len; ++i) {
        // Will trigger Inlining, followed by InstCombine (val * 8 -> val << 3)
        sum += multiply_by_eight(arr[i]); 
    }

    // Candidate for: Constant Folding, Aggressive Dead Code Elimination (ADCE)
    int unused_computation = 100 * 24 / 3; 
    
    // Attempt to trick the compiler into keeping the unused var, 
    // which SROA/Mem2Reg and DCE will eventually clean up.
    if (sum < 0) {
        unused_computation = 0; 
    }

    return sum;
}
