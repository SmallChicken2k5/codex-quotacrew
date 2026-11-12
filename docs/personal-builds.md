# Personal Windows builds

This is an unofficial fork of [QuotaCrew](https://github.com/erkanpulat/codex-quotacrew).
The `personal` branch carries Windows warmup, goal recovery patches and the fork's update channel.

On pushes to `personal` and once a day, the release workflow fetches the latest stable upstream release.
A new build is published only after the merge, checks, Windows package tests and asset verification pass.
Merge conflicts and failed tests leave the last published download available.
Windows build versions use the workflow run number. The release notes identify the upstream tag.

Install the Windows Setup package from this fork's Releases once per machine. While QuotaCrew is running
and automatic updates are enabled in Settings, it checks this fork and installs verified newer builds when
the app can safely restart. Portable copies can be updated by downloading a newer ZIP. Account sign-in and
local Codex history remain on each computer.

For upstream contributions, branch from the original repository's `main` and bring over the relevant
application changes. The release workflow and fork-specific update channel are for this fork.
