cmake_minimum_required(VERSION 3.20)
project(LPTA CXX)

find_package(LLVM REQUIRED CONFIG)
message(STATUS "Using LLVMConfig from: ${LLVM_DIR}")
message(STATUS "LLVM_INCLUDE_DIRS: ${LLVM_INCLUDE_DIRS}")

separate_arguments(LLVM_DEFINITIONS_LIST NATIVE_COMMAND ${LLVM_DEFINITIONS})
add_definitions(${LLVM_DEFINITIONS_LIST})
include_directories(SYSTEM ${LLVM_INCLUDE_DIRS})

add_library(LPTA MODULE LPTA.cpp)
set_target_properties(LPTA PROPERTIES CXX_STANDARD 17 CXX_STANDARD_REQUIRED ON)

if(NOT LLVM_ENABLE_RTTI)
  target_compile_options(LPTA PRIVATE -fno-rtti)
endif()
