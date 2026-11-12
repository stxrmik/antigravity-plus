# The probe app

A tiny app for `tests/live_mobile.py`: a form with two fields, a button that greets and counts, a second screen
with a long list, and a button that crashes on purpose. The same screens on both platforms, so one scenario checks
`mobile.py` on an iOS simulator and on an Android emulator without touching anyone's real app.

- `ios/Probe.swift` — one SwiftUI file, compiled with `swiftc` for the simulator (no Xcode project).
- `android/` — two activities in plain Java, built with the SDK's `aapt2`, `d8`, `zipalign` and `apksigner` (no Gradle).

Bundle id and package: `dev.antigravityplus.probe`. Android also opens `agpprobe://list` (the list screen).
