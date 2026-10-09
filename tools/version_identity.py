"""Stable cloud/local version identity derived from committed package version and Git SHA.

Read-only. Release tags are allocated separately after review; never inferred from a timer.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess

def version_identity(git, sha):
    tags = git('tag', '--points-at', sha).splitlines()
    releases = sorted(t for t in tags if re.fullmatch(r'v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?', t))
    text = git('show', sha + ':pyproject.toml', allowed=(0, 128))
    section = re.search(r'^\[project\]\s*\n(.*?)(?=^\[|\Z)', text, re.M | re.S)
    match = re.search(r'^version\s*=\s*["\x27]([^"\x27]+)', section.group(1) if section else '', re.M)
    package = match.group(1) if match else 'unversioned'
    base = re.sub(r'\.dev(\d+)$', r'-dev.\1', package)
    display = releases[-1] if len(releases) == 1 else base + '+g' + sha
    return {'package_version': package, 'release_tags': releases,
            'build_version': display, 'sha': sha}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--commit', default='HEAD')
    args = parser.parse_args()
    def git(*values, allowed=(0,)):
        p = subprocess.run(['/usr/bin/git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                            '-C', str(args.repo), *values], capture_output=True, text=True, timeout=30)
        if p.returncode not in allowed:
            raise SystemExit('Git read failed: ' + values[0])
        return p.stdout.strip()
    sha = git('rev-parse', '--verify', args.commit + '^{commit}')
    print(json.dumps(version_identity(git, sha), indent=2))
