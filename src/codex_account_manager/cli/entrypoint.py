"""Fast command entry point for terminal status-line callbacks."""

import sys


def main() -> None:
    if sys.argv[1:] == ["antigravity-status"]:
        from codex_account_manager.antigravity import statusline_command

        statusline_command(sys.stdin.buffer, sys.stdout)
        return

    from codex_account_manager.cli.main import app

    app()


if __name__ == "__main__":
    main()
