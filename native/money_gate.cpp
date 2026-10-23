// VENTHORA native reserve gate (C++17).
// Returns 1 when available - debit >= reserve + stress, else 0. Overflow-safe.
//
// Build (Linux/macOS): g++ -std=c++17 -O2 -shared -fPIC money_gate.cpp -o libmoney_gate.so
// Build (Windows):     cl /std:c++17 /O2 /LD money_gate.cpp /Fe:money_gate.dll
// Then set VENTHORA_NATIVE_LIB to the library path.
#include <cstdint>

#if defined(_WIN32)
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

extern "C" EXPORT int venthora_money_gate(int64_t available, int64_t debit, int64_t reserve, int64_t stress) {
    if (available < 0 || debit < 0 || reserve < 0 || stress < 0) return 0;
    if (debit > available) return 0;
    const int64_t left = available - debit;
    if (reserve > left) return 0;
    return stress <= left - reserve ? 1 : 0;
}
