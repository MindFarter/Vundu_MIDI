# Contributing to Vundu_MIDI

Thanks for helping. A few guidelines:

1. Fork, create a branch, and keep each pull request focused on one change.
2. Run `pytest` before you push. Add a test for new API endpoints or DSP code.
3. Audio code runs in the real-time thread: avoid per-sample Python loops, allocations you can
   avoid, file or network access, and locks inside `AudioEngine._render` and effect `_process` methods.
4. New built-in effects subclass `vundu.dsp.Effect`, declare `PARAMS`, and get registered in
   `BUILTIN_EFFECTS`; the GUI builds their strip automatically.
5. Add a line to `CHANGELOG.md` under *Unreleased*.
6. By contributing you agree your work is licensed under GPL-3.0-or-later, like the rest of the project.

Vundu_MIDI is based on [Minihost](https://github.com/shakfu/minihost); issues about plugin loading
itself may belong upstream - please check there and credit its developers.
