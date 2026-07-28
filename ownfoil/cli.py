"""Console-script entrypoint for the `ownfoil` uv tool."""
import argparse
import importlib.resources
import os
import sys


def _app_dir():
    """Directory containing the bundled application code.

    A real `uv tool install`/`uvx` install runs the built wheel, where hatchling's
    force-include copied app/ to ownfoil/_app. An editable/source install (e.g. `uv run
    ownfoil` inside this repo, or running this file directly) never created that folder,
    so fall back to the actual app/ directory sitting next to ownfoil/ in the repo.
    """
    packaged = importlib.resources.files('ownfoil') / '_app'
    if packaged.is_dir():
        return str(packaged)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')


def _write_windows_launcher(base_dir):
    """Drop a double-clickable .bat next to config/data so Windows users don't need a terminal next time."""
    if os.name != 'nt':
        return
    bat_path = os.path.join(base_dir, 'ownfoil.bat')
    if os.path.exists(bat_path):
        return
    with open(bat_path, 'w', newline='\r\n') as f:
        f.write('@echo off\n')
        f.write('uv tool run ownfoil "%~dp0."\n')
        f.write('pause\n')


def main():
    parser = argparse.ArgumentParser(
        prog='ownfoil',
        description='Run Ownfoil locally. A config/ and data/ directory are created inside base_dir.',
    )
    parser.add_argument(
        'base_dir',
        nargs='?',
        default='.',
        help='Directory in which the config/ and data/ folders are created (default: current directory).',
    )
    args = parser.parse_args()

    base_dir = os.path.abspath(args.base_dir)
    os.makedirs(base_dir, exist_ok=True)
    os.environ.setdefault('OWNFOIL_CONFIG_DIR', os.path.join(base_dir, 'config'))
    os.environ.setdefault('OWNFOIL_DATA_DIR', os.path.join(base_dir, 'data'))

    _write_windows_launcher(base_dir)

    sys.path.insert(0, _app_dir())
    if os.name == 'nt':
        from local import main as run_app
    else:
        from run import main as run_app
    run_app()


if __name__ == '__main__':
    main()
