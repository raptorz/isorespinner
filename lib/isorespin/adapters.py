"""Small, checked installer adaptations. No build-host disk probing or chrooting."""
# SPDX-License-Identifier: GPL-3.0-or-later
import ast
import re


class AdapterError(RuntimeError):
    pass


FIRMWARE_HELPER = '''
# isorespin IA32 adapter: preserve the original path on BIOS and 64-bit UEFI.
def _isorespin_ia32():
    try:
        with open('/sys/firmware/efi/fw_platform_size') as stream:
            return stream.read().strip() == '32'
    except FileNotFoundError:
        return False

def _isorespin_target(action, target):
    import subprocess
    subprocess.run(['/usr/lib/isorespin/install-ia32', action, target], check=True)
'''


def function(tree, name):
    matches = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if len(matches) != 1:
        raise AdapterError(f"Expected exactly one {name} function, found {len(matches)}")
    return matches[0]


def insert_before_body(source, name, code):
    tree = ast.parse(source)
    node = function(tree, name)
    body = node.body
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if not body:
        raise AdapterError(f"Empty function {name}")
    lines = source.splitlines(keepends=True)
    indent = ' ' * body[0].col_offset
    lines[body[0].lineno - 1:body[0].lineno - 1] = [indent + line + '\n' for line in code.splitlines()]
    result = ''.join(lines)
    ast.parse(result)
    return result


def patch_curthooks(source):
    """Adapt before dependency installation and at the actual bootloader stage."""
    if 'isorespin IA32 adapter' in source:
        raise AdapterError('curtin is already adapted; use an original ISO')
    tree = ast.parse(source)
    node = function(tree, 'install_missing_packages')
    block = '\n'.join(source.splitlines()[node.lineno - 1:node.end_lineno])
    # Override the EFI package list after all upstream signed/shim selections.
    pattern = r'(?m)^(\s*)needed_packages\.update\(\[pkg for pkg in uefi_pkgs'
    if len(re.findall(pattern, block)) != 1:
        raise AdapterError('Unrecognised curtin EFI dependency selection; refusing an unchecked patch')
    block = re.sub(pattern, lambda m: m[1] + "if _isorespin_ia32() and osfamily == DISTROS.debian:\n" +
                   m[1] + "    uefi_pkgs = ['grub-efi-ia32-bin', 'grub2-common', 'efibootmgr']\n" + m[0], block)
    lines = source.splitlines()
    lines[node.lineno - 1:node.end_lineno] = block.splitlines()
    source = '\n'.join(lines) + '\n'
    source = insert_before_body(source, 'install_missing_packages',
                                "if _isorespin_ia32() and osfamily == DISTROS.debian:\n"
                                "    _isorespin_target('prepare', target)")
    source = insert_before_body(source, 'setup_grub',
                                "if _isorespin_ia32() and osfamily == DISTROS.debian:\n"
                                "    _isorespin_target('install', target)\n"
                                "    return")
    source += FIRMWARE_HELPER
    ast.parse(source)
    return source


def patch_install_grub(source):
    return insert_before_body(source, 'get_grub_package_name',
                              "if target_arch == 'amd64' and uefi and _isorespin_ia32():\n"
                              "    return ('grub-efi-ia32-bin', 'i386-efi')") + FIRMWARE_HELPER


def ubiquity_wrapper():
    # Preserve the installer's debconf protocol by delegating on other firmware.
    # On IA32 this is the bootloader phase itself, not a post-success hook.
    return '''#!/bin/sh
set -eu
if [ "$(cat /sys/firmware/efi/fw_platform_size 2>/dev/null || true)" = 32 ]; then
    [ -n "${1:-}" ] || { echo 'isorespin: missing installer target' >&2; exit 1; }
    exec /usr/lib/isorespin/install-ia32 install "$1"
fi
exec /usr/share/grub-installer/grub-installer.isorespin-original "$@"
'''


def unassert_seed(seed, filenames):
    """A modified snap must NOT retain a signed-seed claim for its old digest."""
    found = set()
    for snap in seed.get('snaps', []):
        if snap.get('file') in filenames:
            snap['unasserted'] = True
            snap.pop('id', None)
            snap.pop('snap-id', None)
            snap.pop('revision', None)
            found.add(snap['file'])
    if found != set(filenames):
        raise AdapterError('Modified installer snaps were not all referenced in seed.yaml')
    return seed
