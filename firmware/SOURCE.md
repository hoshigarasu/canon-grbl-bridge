# firmware/grblHAL_UNO_Q.elf — Provenance

This ELF is a **CMake Debug build** of
[grblHAL-STM32U585](https://github.com/hoshigarasu/grblHAL-STM32U585).

## Current build

| Field | Value |
|-------|-------|
| Source commit | [`428b19f`](https://github.com/hoshigarasu/grblHAL-STM32U585/commit/428b19f) |
| Toolchain | arm-none-eabi-gcc 13.2.1 (Ubuntu apt: gcc-arm-none-eabi 15:13.2.rel1-2) |
| Build system | CMake (cmake/arm-none-eabi.cmake) — CubeIDE非依存 |
| Build type | Debug -O0 -g3 (3.8 MB ELF; OpenOCD writes only the ~330 KB loadable sections) |
| Preprocessor defines | `DEBUG`, `BOARD_UNO_Q_CNC`, `COREXY=1`, `USE_HAL_DRIVER`, `STM32U585xx` |
| Branch | feature/triac-direct-control |
| 含むコミット | fix(triac): PA0 internal pull-up for TEMP NTC (428b19f) / M817 ADC diag (f377f9f) |

## Updating this file

Whenever `firmware/grblHAL_UNO_Q.elf` is replaced:

1. Build from a `grblHAL-STM32U585` checkout:
   - CMake: `cmake -B build-cmake -DCMAKE_TOOLCHAIN_FILE=cmake/arm-none-eabi.cmake && cmake --build build-cmake`
   - CubeIDE: see `BUILD.md` for required preprocessor defines and HAL modules.
2. Update the **Source commit** row above to the `grblHAL-STM32U585` commit
   the build was made from.
3. Commit the new `.elf` together with this file in the same commit, with a
   message of the form:

   ```
   firmware: update grblHAL_UNO_Q.elf (grblHAL-STM32U585@<short-hash>)
   ```

This keeps the binary traceable to its source, even though the two
repositories have no submodule/CI link.
