# firmware/grblHAL_UNO_Q.elf — Provenance

This ELF is a CubeIDE Debug build of
[grblHAL-STM32U585](https://github.com/hoshigarasu/grblHAL-STM32U585).

## Current build

| Field | Value |
|-------|-------|
| Source commit | [`29938dc`](https://github.com/hoshigarasu/grblHAL-STM32U585/commit/29938dc) |
| Toolchain | GNU Tools for STM32 14.3.rel1.20251027-0700 (arm-none-eabi-gcc 14.3.1) |
| Build type | Debug (3.8 MB, includes debug symbols; OpenOCD writes only the ~300 KB loadable sections) |
| Preprocessor defines | `BOARD_UNO_Q_CNC`, `COREXY=1` (see grblHAL-STM32U585 `BUILD.md`) |

## Updating this file

Whenever `firmware/grblHAL_UNO_Q.elf` is replaced:

1. Build from a clean `grblHAL-STM32U585` checkout in STM32CubeIDE
   (see that repo's `BUILD.md` for required preprocessor defines and HAL modules).
2. Update the **Source commit** row above to the `grblHAL-STM32U585` commit
   the build was made from.
3. Commit the new `.elf` together with this file in the same commit, with a
   message of the form:

   ```
   firmware: update grblHAL_UNO_Q.elf (grblHAL-STM32U585@<short-hash>)
   ```

This keeps the binary traceable to its source, even though the two
repositories have no submodule/CI link.
