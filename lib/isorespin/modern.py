#!/usr/bin/env python3
"""Content-based, minimal IA32 remastering alongside the original 8.7.1 path.

Never run target programs during a build. Only installer bootloader stages run
install-ia32, on the machine being installed. Squashfs layers are edited in place,
not flattened; original kernels, initrds, source selections and package databases
are retained. A successful build is NOT a claim of hardware validation.
"""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

from adapters import AdapterError, patch_curthooks, patch_install_grub, ubiquity_wrapper, unassert_seed, insert_before_body

LIB = Path(__file__).resolve().parent
SCRIPT = LIB.parent.parent / 'isorespin.sh'
LEGACY = 78


def run(argv, *, check=True, cwd=None, env=None):
    result = subprocess.run([str(a) for a in argv], cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {shlex.join(map(str, argv))}\n"
                           + result.stderr.decode(errors='replace')[-10000:]
                           + result.stdout.decode(errors='replace')[-10000:])
    return result


def say(message):
    print('isorespin: ' + message, flush=True)


def require(commands):
    missing = [c for c in commands if not shutil.which(c)]
    if missing:
        raise RuntimeError('Missing tools: ' + ', '.join(missing))


def sha(path, algorithm='sha256'):
    digest = hashlib.new(algorithm)
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f'Refusing to overwrite a symlink: {path}')
    path.write_text(text)
    path.chmod(mode)


def os_release(text):
    result = {}
    for line in text.splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            values = shlex.split(value)
            result[key] = ' '.join(values)
    return result


def parse_lbas(text):
    result = {}
    for line in text.splitlines():
        match = re.match(r'File data lba:\s*(\d+)\s*,\s*(\d+)\s*,\s*\d+\s*,\s*(\d+)\s*,\s*(.*)', line)
        if not match:
            continue
        path = shlex.split(match[4])[0]
        if path in result or int(match[1]) != 0:
            raise RuntimeError(f'Multi-extent ISO file not supported: {path}')
        result[path] = (int(match[2]) * 2048, int(match[3]))
    return result


class ISO:
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        report = run(['xorriso', '-indev', self.path, '-find', '/', '-type', 'f', '-exec', 'report_lba', '--'])
        self.files = parse_lbas(report.stdout.decode())
        self.layers = sorted(p for p in self.files if p.startswith('/casper/') and p.endswith('.squashfs'))

    def read(self, path, limit=8 * 1024 * 1024):
        offset, size = self.files[path]
        if size > limit:
            raise RuntimeError(f'Unexpectedly large metadata file: {path}')
        with self.path.open('rb') as stream:
            stream.seek(offset)
            data = stream.read(size)
        if len(data) != size:
            raise RuntimeError(f'Truncated ISO file: {path}')
        return data

    def sq(self, layer, *args, check=False):
        return run(['unsquashfs', *args, '-o', self.files[layer][0], self.path], check=check)

    def cat(self, layer, path):
        result = run(['unsquashfs', '-cat', '-o', self.files[layer][0], self.path, path], check=False)
        return result.stdout if result.returncode == 0 else b''

    def paths(self, layer, *paths):
        result = run(['unsquashfs', '-ls', '-o', self.files[layer][0], self.path, *paths], check=False)
        return [line[len('squashfs-root/'):] for line in result.stdout.decode().splitlines()
                if line.startswith('squashfs-root/')]


def profile(info, release):
    distro, version = release.get('ID'), release.get('VERSION_ID')
    if distro == 'linuxmint' and version == '20.3':
        return 'mint20-legacy'
    if distro == 'linuxmint' and version == '22.3':
        if release.get('ISORESPIN_EDITION', '').lower() not in ('cinnamon', 'xfce'):
            raise RuntimeError('Only Mint 22.3 Cinnamon and Xfce are in scope')
        if release.get('UBUNTU_CODENAME') != 'noble':
            raise RuntimeError('Unexpected Ubuntu base for Mint 22.3')
        return 'mint22-ubiquity'
    if distro == 'ubuntu' and version == '26.04':
        if not re.match(r'\s*(Ubuntu|Xubuntu)\s+26\.04(?:\D|$)', info, re.I) or re.search('server', info, re.I):
            raise RuntimeError('Only Ubuntu/Xubuntu 26.04 desktop images are in scope')
        return 'ubuntu26-subiquity'
    return 'legacy'


def inspect(iso):
    info = iso.read('/.disk/info').decode().strip()
    if not re.search(r'\b(amd64|x86_64|64-bit)\b', info, re.I):
        raise RuntimeError('Not an amd64 desktop image')
    if not iso.layers:
        raise RuntimeError('No Casper squashfs files found')
    base = next((p for p in ('/casper/filesystem.squashfs', '/casper/minimal.squashfs') if p in iso.layers), None)
    if not base:
        raise RuntimeError('Unrecognised Casper base layer')
    data = iso.cat(base, 'usr/lib/os-release') or iso.cat(base, 'etc/os-release')
    release = os_release(data.decode())
    if release.get('ID') == 'linuxmint':
        mint = os_release(iso.cat(base, 'etc/linuxmint/info').decode())
        release['ISORESPIN_EDITION'] = mint.get('EDITION', '')
    kind = profile(info, release)
    result = {'image': str(iso.path), 'info': info, 'profile': kind, 'release': release,
              'layers': iso.layers, 'kernel_configs': {}, 'installer_layers': [],
              'validation': 'inspection only; no firmware or installation test'}
    if kind in ('legacy', 'mint20-legacy'):
        return result
    for layer in iso.layers:
        for path in iso.paths(layer, 'boot/config-*', 'usr/lib/modules/*/config'):
            if re.fullmatch(r'boot/config-[^/]+|usr/lib/modules/[^/]+/config', path):
                config = iso.cat(layer, path).decode()
                result['kernel_configs'][path] = {key: f'{key}=y' in config.splitlines() for key in
                                                  ('CONFIG_X86_64', 'CONFIG_EFI_MIXED', 'CONFIG_EFI_STUB', 'CONFIG_EFI_HANDOVER_PROTOCOL')}
        found = iso.paths(layer, 'usr/share/grub-installer/grub-installer',
                          'var/lib/snapd/seed/seed.yaml', 'var/lib/snapd/seed/snaps/*bootstrap*.snap',
                          'var/lib/snapd/seed/snaps/subiquity*.snap')
        found = [p for p in found if p.endswith(('/grub-installer', '/seed.yaml', '.snap'))]
        if found:
            result['installer_layers'].append({'layer': layer, 'paths': found})
    return result


def check_kernel(report):
    configs = report['kernel_configs']
    if not configs:
        raise RuntimeError('Cannot verify mixed-mode kernel support: no kernel config found in the image')
    for path, flags in configs.items():
        missing = [name for name, enabled in flags.items() if not enabled]
        if missing:
            raise RuntimeError(f'{path} lacks required kernel capabilities: {", ".join(missing)}')


def compression(path):
    text = run(['unsquashfs', '-s', path]).stdout.decode()
    comp = re.search(r'^Compression (\w+)', text, re.M)
    block = re.search(r'^Block size (\d+)', text, re.M)
    if not comp or not block:
        raise RuntimeError(f'Cannot determine squashfs settings: {path}')
    return comp[1], block[1]


def pack_squash(root, original, destination):
    comp, block = compression(original)
    run(['mksquashfs', root, destination, '-noappend', '-no-progress', '-comp', comp,
         '-b', block, '-processors', '2', '-mem', '256M'])


def apt_repo(work, suite, destination, target_statuses=()):
    """Resolve a complete amd64 dependency closure, isolated from host APT state."""
    apt = work / 'apt-state'
    for directory in ('etc/parts', 'state/lists/partial', 'cache/archives/partial', 'log'):
        (apt / directory).mkdir(parents=True)
    write(apt / 'state/status', '')  # Empty status: host-installed packages must NOT suppress downloads.
    key = Path('/usr/share/keyrings/ubuntu-archive-keyring.gpg')
    if not key.is_file():
        raise RuntimeError('Install ubuntu-keyring on the build host')
    write(apt / 'etc/sources.list', '\n'.join(
        f'deb [arch=amd64 signed-by={key}] http://archive.ubuntu.com/ubuntu {pocket} main universe restricted multiverse'
        for pocket in (suite, suite + '-updates', suite + '-security')) + '\n')
    config = apt / 'apt.conf'
    write(config, f'''Dir {json.dumps(str(apt))};
Dir::Etc "etc";
Dir::Etc::sourcelist "sources.list";
Dir::Etc::sourceparts "parts";
Dir::Etc::parts "parts";
Dir::Etc::main "-";
Dir::Etc::preferences "-";
Dir::Etc::preferencesparts "parts";
Dir::State "state";
Dir::State::status "status";
Dir::Cache "cache";
Dir::Log "log";
APT::Architecture "amd64";
APT::Architectures {{ "amd64"; }};
APT::Install-Recommends "false";
Acquire::Languages "none";
Acquire::Retries "3";
Acquire::http::Timeout "30";
Acquire::https::Timeout "30";
''')
    env = dict(os.environ, APT_CONFIG=str(config), LC_ALL='C')
    say(f'Resolving IA32 GRUB dependencies from {suite} (isolated APT state) ...')
    run(['apt-get', '-o', 'APT::Update::Error-Mode=any', 'update'], env=env)
    # The active-loader metapackage conflicts with grub-pc, which is required by
    # Mint's signed GRUB/shim/installer dependency chain. The IA32 modules can
    # coexist; our small trigger package maintains the installed EFI images.
    download = ['apt-get', '--download-only', '--no-install-recommends', '--no-remove', '-y', 'install',
                'grub-efi-ia32-bin', 'grub-common', 'grub2-common', 'efibootmgr', 'util-linux']
    run(download, env=env)
    # Empty status supplies the forward closure; real target states additionally
    # account for installed packages' Breaks/Conflicts during a GRUB upgrade.
    for status in target_statuses:
        write(apt / 'state/status', status)
        run(download, env=env)
    destination.mkdir(parents=True)
    packages = list((apt / 'cache/archives').glob('*.deb'))
    if not packages:
        raise RuntimeError('APT did not download any packages')
    inventory = []
    for package in packages:
        fields = run(['dpkg-deb', '-f', package, 'Package', 'Version', 'Architecture']).stdout.decode()
        inventory.append({'file': package.name, 'fields': fields, 'sha256': sha(package)})
        shutil.copy2(package, destination / package.name)
    maintenance = maintenance_package(work, destination)
    inventory.append({'file': maintenance.name, 'origin': 'local isorespin IA32 maintenance',
                      'sha256': sha(maintenance)})
    index = run(['dpkg-scanpackages', '--multiversion', '.', '/dev/null'], cwd=destination).stdout.decode()
    write(destination / 'Packages', index)
    write(destination / 'SHA256SUMS', ''.join(f'{sha(p)}  {p.name}\n' for p in sorted(destination.iterdir()) if p.is_file()))
    # Test the actual offline repository, with no archive lists available to
    # accidentally satisfy dependencies missing from the final ISO.
    say('Checking offline IA32 package installation against image package states ...')
    offline = apt / 'state/offline-lists'
    (offline / 'partial').mkdir(parents=True)
    write(apt / 'etc/sources.list', f'deb [trusted=yes] file:{destination} ./\n')
    options = ['-o', 'Dir::State::lists=' + str(offline)]
    run(['apt-get', *options, '-o', 'APT::Update::Error-Mode=any', 'update'], env=env)
    for status in ('', *target_statuses):
        write(apt / 'state/status', status)
        run(['apt-get', *options, '--simulate', '--no-remove', '--no-install-recommends',
             'install', 'isorespin-ia32'], env=env)
    return inventory


def maintenance_package(work, destination):
    """Maintain IA32 EFI/module consistency without replacing distro packages."""
    root = work / 'ia32-maintenance-package'
    write(root / 'DEBIAN/control', '''Package: isorespin-ia32
Version: 1.0
Architecture: all
Maintainer: isorespin local build <isorespin@localhost>
Depends: grub-efi-ia32-bin, grub2-common, efibootmgr, util-linux
Section: admin
Priority: optional
Description: Local IA32 UEFI bootloader maintenance for isorespin installations
 Refreshes installed IA32 GRUB images after GRUB package and kernel updates.
 Activated only after the installer has completed an IA32 bootloader install.
''')
    write(root / 'DEBIAN/triggers', '''interest-noawait /usr/lib/grub
interest-noawait /usr/sbin/grub-install
interest-noawait /usr/sbin/grub-mkconfig
''')
    write(root / 'DEBIAN/postinst', '''#!/bin/sh
set -eu
case "${1:-}" in
    configure|triggered) /usr/lib/isorespin/refresh-ia32 ;;
esac
''', 0o755)
    write(root / 'usr/lib/isorespin/refresh-ia32', (LIB / 'refresh-ia32').read_text(), 0o755)
    write(root / 'etc/kernel/postinst.d/zz-isorespin-ia32', '''#!/bin/sh
set -eu
exec /usr/lib/isorespin/refresh-ia32
''', 0o755)
    package = destination / 'isorespin-ia32_1.0_all.deb'
    run(['dpkg-deb', '--root-owner-group', '--build', root, package])
    return package


def pe_ia32(path):
    data = path.read_bytes()
    if len(data) < 64 or data[:2] != b'MZ':
        return False
    offset, = struct.unpack_from('<I', data, 60)
    return offset + 6 <= len(data) and data[offset:offset + 6] == b'PE\0\0\x4c\x01'


def legacy_bootkit(destination):
    payload = SCRIPT.read_bytes().split(b'\nexit 0\n', 1)[1]
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload))) as archive:
        # Extract only the tested loader and its matching modules, no driver scripts.
        for name in archive.namelist():
            if name in ('grub_bootia32.efi', 'efi_bootia32.efi') or name.startswith('grub/i386-efi/'):
                if '..' in PurePosixPath(name).parts:
                    raise RuntimeError('Unsafe embedded archive member')
                archive.extract(name, destination)
    for name in ('grub_bootia32.efi', 'efi_bootia32.efi'):
        if not pe_ia32(destination / name):
            raise RuntimeError('Invalid embedded IA32 loader')


def boot_layout(iso):
    text = run(['xorriso', '-indev', iso.path, '-report_el_torito', 'plain']).stdout.decode()
    entries = re.findall(r'^El Torito boot img\s*:\s*(\d+)\s+UEFI\s+\w+\s+\w+\s+\S+\s+\S+\s+(\d+)\s+(\d+)', text, re.M)
    if len(entries) != 1:
        raise RuntimeError('Expected exactly one EFI boot image')
    number, sectors, lba = map(int, entries[0])
    if number != max(map(int, re.findall(r'^El Torito boot img\s*:\s*(\d+)', text, re.M))):
        raise RuntimeError('EFI must be the last El Torito entry for this replay adapter')
    match = re.search(rf'^El Torito img path\s*:\s*{number}\s+(.+)$', text, re.M)
    path = match[1].strip() if match else None
    if path and not path.startswith('/'):  # Appended-partition interval, not an ISO file.
        path = None
    # Replay can turn a GPT-exposed file into an appended partition. Consult the
    # replay plan even when the plain report still shows an ISO path.
    mkisofs = run(['xorriso', '-indev', iso.path, '-report_el_torito', 'as_mkisofs']).stdout.decode()
    parts = re.findall(r'(?m)^-append_partition\s+(\d+)\s+(\S+)\s+', mkisofs)
    efi_parts = [int(n) for n, kind in parts if kind.lower() in ('0xef', '28732ac11ff8d211ba4b00a0c93ec93b')]
    if len(efi_parts) > 1 or (path is None and not efi_parts):
        raise RuntimeError('Unrecognised appended EFI partition:\n' + text + '\n' + mkisofs)
    appended = efi_parts[0] if efi_parts else None
    return {'path': path, 'offset': lba * 2048, 'size': sectors * 512, 'appended': appended}


def replay_command(iso, tree, output, layout, esp):
    # Keep unchanged boot-file objects; -map would replace their ISO inodes and
    # leave a hidden, stale BIOS image on hybrid Mint. Explicitly reset EFI below.
    command = ['xorriso', '-indev', iso.path, '-outdev', output,
               '-boot_image', 'any', 'replay', '-update_r', tree, '/']
    if layout['appended'] is not None:
        number = layout['appended']
        command += ['-append_partition', str(number), '0xef', esp,
                    '-boot_image', 'any', f'efi_path=--interval:appended_partition_{number}:all::',
                    '-boot_image', 'any', 'load_size=full']
    else:
        command += ['-boot_image', 'any', f"efi_path={layout['path']}",
                    '-boot_image', 'any', 'load_size=full']
    return command + ['-commit', '-end']


def add_bootkit(iso, tree, work, layout):
    kit = work / 'bootkit'
    kit.mkdir()
    legacy_bootkit(kit)
    shutil.copytree(kit / 'grub/i386-efi', tree / 'boot/grub/i386-efi', dirs_exist_ok=True)
    bootdirs = [p for p in (tree / 'EFI').iterdir() if p.name.lower() == 'boot']
    if len(bootdirs) != 1:
        raise RuntimeError('Cannot identify EFI/BOOT directory')
    shutil.copy2(kit / 'efi_bootia32.efi', bootdirs[0] / 'bootia32.efi')
    # Legacy images contain all startup modules. Keep the module path local to the
    # boot medium; this configuration is NEVER copied to the installed ESP.
    cfg = 'search --no-floppy --file --set=root /.disk/info\nset prefix=($root)/boot/grub\nconfigfile $prefix/grub.cfg\n'
    write(tree / 'boot/grub/i386-efi/grub.cfg', cfg)
    original = work / 'efi-original.img'
    with iso.path.open('rb') as stream, original.open('wb') as out:
        stream.seek(layout['offset'])
        remaining = layout['size']
        while remaining:
            data = stream.read(min(remaining, 1024 * 1024))
            if not data:
                raise RuntimeError('Truncated EFI boot image')
            out.write(data)
            remaining -= len(data)
    esp_tree = work / 'esp-tree'
    esp_tree.mkdir()
    run(['mcopy', '-s', '-i', original, '::*', esp_tree])
    esp = work / 'efi-ia32.img'
    size = max(16 * 1024 * 1024, layout['size'] + 4 * 1024 * 1024)
    with esp.open('wb') as stream:
        stream.truncate(size)
    run(['mformat', '-i', esp, '::'])
    for path in esp_tree.iterdir():
        run(['mcopy', '-s', '-i', esp, path, '::/'])
    run(['mcopy', '-o', '-i', esp, kit / 'grub_bootia32.efi', '::/EFI/BOOT/BOOTIA32.EFI'])
    # Also place a configuration in the FAT image for firmware exposing it as root.
    for directory in ('::/boot', '::/boot/grub'):
        run(['mmd', '-i', esp, directory], check=False)
    fat_cfg = work / 'grub.cfg'
    write(fat_cfg, cfg)
    run(['mcopy', '-o', '-i', esp, fat_cfg, '::/boot/grub/grub.cfg'])
    if layout['path']:
        shutil.copy2(esp, tree / layout['path'].lstrip('/'))
    return esp


def remove_bytecode(path):
    for cache in path.parent.glob('__pycache__/' + path.stem + '.*.pyc'):
        cache.unlink()
    path.with_suffix('.pyc').unlink(missing_ok=True)


def adapt_snap(path, work):
    import yaml
    root = work / ('snap-' + path.stem)
    run(['unsquashfs', '-no-progress', '-d', root, path])
    metadata = yaml.safe_load((root / 'meta/snap.yaml').read_text())
    if metadata.get('confinement') != 'classic':
        raise RuntimeError('Installer adapter requires a classic-confinement snap')
    hooks = list(root.glob('**/curtin/commands/curthooks.py'))
    if not hooks:
        raise RuntimeError(f'Installer snap has no recognised curtin source: {path.name}')
    for hook in hooks:
        write(hook, patch_curthooks(hook.read_text()), hook.stat().st_mode & 0o7777)
        install = hook.with_name('install_grub.py')
        write(install, patch_install_grub(install.read_text()), install.stat().st_mode & 0o7777)
        remove_bytecode(hook)
        remove_bytecode(install)
    refreshes = list(root.glob('**/subiquity/server/controllers/refresh.py'))
    if not refreshes:
        raise RuntimeError('Cannot disable installer self-refresh in adapted snap')
    for refresh in refreshes:
        source = insert_before_body(refresh.read_text(), 'check_for_update',
                                    "self.status.availability = RefreshCheckState.UNAVAILABLE\nreturn")
        source = insert_before_body(source, 'start_update',
                                    "raise RuntimeError('Installer refresh is disabled on this IA32-adapted medium')")
        write(refresh, source, refresh.stat().st_mode & 0o7777)
        remove_bytecode(refresh)
    # Unsigned/local snaps must be explicitly marked as such in seed.yaml.
    rebuilt = work / ('adapted-' + path.name)
    pack_squash(root, path, rebuilt)
    shutil.copy2(rebuilt, path)
    shutil.rmtree(root)
    rebuilt.unlink()


def adapt_layers(iso, report, tree, work):
    import yaml
    staged = {}
    modified_snaps = set()
    seed_paths = []
    ubiquity_count = 0
    changes = []
    # Only layers containing installer components are unpacked. Casper's layer
    # graph, language variants, whiteouts and install source selection stay intact.
    for entry in report['installer_layers']:
        layer = entry['layer']
        root = work / ('layer-' + Path(layer).stem)
        source = tree / layer.lstrip('/')
        say(f'Adapting installer layer {Path(layer).name} ...')
        run(['unsquashfs', '-no-progress', '-d', root, source])
        staged[layer] = (root, source)
        script = root / 'usr/share/grub-installer/grub-installer'
        if script.is_file():
            original = script.with_name(script.name + '.isorespin-original')
            if original.exists():
                raise RuntimeError('Input already has an isorespin installer adapter')
            script.rename(original)
            write(script, ubiquity_wrapper(), 0o755)
            ubiquity_count += 1
        seed = root / 'var/lib/snapd/seed/seed.yaml'
        if seed.is_file():
            if any(seed.parent.glob('preseed*')):
                raise RuntimeError('Preseeded snap state requires a separate adapter; refusing to invalidate it')
            seed_paths.append(seed)
        for snap in sorted((root / 'var/lib/snapd/seed/snaps').glob('*.snap')):
            if re.match(r'(ubuntu-desktop-bootstrap|subiquity)_', snap.name):
                say(f'Adapting {snap.name} (local, unasserted installer snap) ...')
                adapt_snap(snap, work)
                modified_snaps.add(snap.name)
        write(root / 'usr/lib/isorespin/install-ia32', (LIB / 'install-ia32').read_text(), 0o755)
    if report['profile'] == 'mint22-ubiquity' and not ubiquity_count:
        raise RuntimeError('Mint image has no Ubiquity grub-installer')
    if report['profile'] == 'ubuntu26-subiquity' and not modified_snaps:
        raise RuntimeError('Ubuntu image has no supported seeded installer snap')
    referenced = set()
    for seed in seed_paths:
        data = yaml.safe_load(seed.read_text())
        files = {snap.get('file') for snap in data.get('snaps', [])} & modified_snaps
        if files:
            write(seed, yaml.safe_dump(unassert_seed(data, files), sort_keys=False))
            referenced.update(files)
    if referenced != modified_snaps:
        raise RuntimeError('Not all modified snaps have an adapted seed.yaml')
    for layer, (root, source) in staged.items():
        sudo = root / 'usr/bin/sudo'
        if sudo.exists() and not (sudo.stat().st_mode & 0o4000):
            raise RuntimeError('sudo lost its setuid bit; refusing to pack a broken desktop')
        rebuilt = work / (source.name + '.new')
        pack_squash(root, source, rebuilt)
        shutil.copy2(rebuilt, source)
        size_path = source.with_suffix('.size')
        size = int(run(['du', '-sx', '--block-size=1', root]).stdout.split()[0])
        old_size = int(size_path.read_text().strip()) if size_path.is_file() else size
        write(size_path, str(size) + '\n')
        changes.append({'layer': layer, 'size_delta': size - old_size, 'sha256': sha(source)})
        shutil.rmtree(root)
        rebuilt.unlink()
    # Package manifests are deliberately unchanged: no packages are installed or
    # removed in the image. Refresh approximate installed sizes in the catalog.
    catalog = tree / 'casper/install-sources.yaml'
    if catalog.exists():
        data = yaml.safe_load(catalog.read_text())
        def update_sizes(node):
            if isinstance(node, dict):
                path = node.get('path', '')
                if isinstance(path, str) and path.endswith('.squashfs') and isinstance(node.get('size'), int):
                    leaf = Path(path).name.removesuffix('.squashfs')
                    for change in changes:
                        base = Path(change['layer']).name.removesuffix('.squashfs')
                        if leaf == base or leaf.startswith(base + '.'):
                            node['size'] += change['size_delta']
                for value in node.values():
                    update_sizes(value)
            elif isinstance(node, list):
                for value in node:
                    update_sizes(value)
        update_sizes(data)
        write(catalog, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return changes


def checksums(tree, catalog):
    excluded = {'md5sum.txt', 'isolinux/isolinux.bin', 'boot/grub/i386-pc/eltorito.img'}
    if catalog:
        excluded.add(catalog.lstrip('/'))
    lines = []
    for path in sorted(tree.rglob('*')):
        relative = path.relative_to(tree).as_posix()
        if path.is_file() and not path.is_symlink() and relative not in excluded:
            lines.append(f'{sha(path, "md5")}  ./{relative}\n')
    write(tree / 'md5sum.txt', ''.join(lines))


def build(iso, report, args):
    if sys.version_info < (3, 10):
        raise RuntimeError('Modern builds require Python 3.10 or newer')
    import yaml  # Check dependency before any expensive extraction.
    del yaml
    require(['xorriso', 'unsquashfs', 'mksquashfs', 'mcopy', 'mformat', 'mmd',
             'apt-get', 'dpkg-deb', 'dpkg-scanpackages', 'du'])
    if os.geteuid() != 0:
        raise RuntimeError('Full builds require root to preserve ownership, setuid and xattrs. '
                           'Use sudo ./isorespin.sh -i ISO (sudo xorriso alone is insufficient). '
                           '--inspect needs no sudo.')
    check_kernel(report)
    parent = Path(args.work_directory).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    output = parent / ('linuxium-' + iso.path.name)
    sidecar = output.with_suffix('.iso.build.json')
    if output.exists() or sidecar.exists():
        raise RuntimeError(f'Output already exists; not overwriting: {output}')
    needed = max(20 * 1024**3, iso.path.stat().st_size * 5)
    if shutil.disk_usage(parent).free < needed:
        raise RuntimeError(f'Need at least {needed // 1024**3} GiB free in {parent}')
    # Unique directory: never delete a fixed user work directory or original ISO.
    work = Path(tempfile.mkdtemp(prefix='isorespin-build-', dir=parent))
    work.chmod(0o755)
    say(f'Work directory: {work} (retained on failure)')
    tree = work / 'iso'
    run(['xorriso', '-osirrox', 'on', '-indev', iso.path, '-extract', '/', tree])
    if (tree / 'isorespin/build.json').exists():
        raise RuntimeError('Use an original distribution ISO, not an already respun image')
    report['input_sha256'] = sha(iso.path)
    statuses = []
    for layer in iso.layers:
        if Path(layer).name in ('filesystem.squashfs', 'minimal.squashfs', 'minimal.standard.squashfs'):
            status = iso.cat(layer, 'var/lib/dpkg/status').decode()
            if status:
                statuses.append(status)
    if not statuses:
        raise RuntimeError('Cannot read the target package database for offline dependency resolution')
    report['packages'] = apt_repo(work, 'noble' if report['profile'].startswith('mint') else 'resolute',
                                  tree / 'isorespin/apt', statuses)
    report['changes'] = adapt_layers(iso, report, tree, work)
    layout = boot_layout(iso)
    esp = add_bootkit(iso, tree, work, layout)
    report['ia32_loader'] = 'original isorespin 8.7.1 / GRUB 2.04 (hardware validation required)'
    report['validation'] = 'build and structural checks only; Live/install/reboot not tested by this build'
    write(tree / 'isorespin/build.json', json.dumps(report, indent=2) + '\n')
    write(tree / 'README.isorespin', 'isorespin IA32 adapters\nOriginal kernel/initrd preserved.\n'
          'No Atom driver injection. Secure Boot must be disabled for IA32.\n'
          'See /isorespin/build.json. Hardware validation is still required.\n')
    boot_report = run(['xorriso', '-indev', iso.path, '-report_el_torito', 'plain']).stdout.decode()
    cat_match = re.search(r'^El Torito cat path\s*:\s*(.+)$', boot_report, re.M)
    checksums(tree, cat_match[1].strip() if cat_match else None)
    partial = work / 'output.partial.iso'
    # Replay includes the ORIGINAL complete system area (including APM), not a
    # truncated 446-byte MBR template. Resolve it before replacing file objects.
    command = replay_command(iso, tree, partial, layout, esp)
    say('Replaying input ISO boot layout and writing image ...')
    run(command)
    verify = ISO(partial)
    verified_layout = boot_layout(verify)
    boot_file = next((p for p in verify.files if p.lower() == '/efi/boot/bootia32.efi'), None)
    if not boot_file or verify.read(boot_file) != (tree / boot_file.lstrip('/')).read_bytes():
        raise RuntimeError('Output IA32 loader verification failed')
    # Check the ACTUAL output EFI image, not just its ISO-visible duplicate.
    verify_esp = work / 'verify-esp.img'
    with partial.open('rb') as stream, verify_esp.open('wb') as out:
        stream.seek(verified_layout['offset'])
        out.write(stream.read(verified_layout['size']))
    loader = work / 'verify-bootia32.efi'
    run(['mcopy', '-i', verify_esp, '::/EFI/BOOT/BOOTIA32.EFI', loader])
    if not pe_ia32(loader):
        raise RuntimeError('Output ESP lacks a valid IA32 executable')
    # Atomic no-clobber publication, even if another build selected the same name.
    os.link(partial, output)
    with sidecar.open('x') as stream:
        stream.write(json.dumps(report, indent=2) + '\n')
    say(f'Created {output}; physical Live/install/reboot validation is still required.')
    if not args.keep_work:
        shutil.rmtree(work)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dispatch = '--dispatch' in argv
    if dispatch:
        argv.remove('--dispatch')
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('-i', '--iso')
    pre.add_argument('--inspect', action='store_true')
    known, _ = pre.parse_known_args(argv)
    if not known.iso:
        if known.inspect:
            raise RuntimeError('--inspect requires -i ISO')
        return LEGACY if dispatch else 2
    require(['xorriso', 'unsquashfs'])
    iso = ISO(known.iso)
    report = inspect(iso)
    if known.inspect:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    if report['profile'] in ('legacy', 'mint20-legacy'):
        return LEGACY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-i', '--iso', required=True)
    parser.add_argument('-w', '--work-directory', default='.')
    parser.add_argument('--keep-work', action='store_true')
    parser.add_argument('--inspect', action='store_true')
    args = parser.parse_args(argv)  # Never silently ignore legacy customization options.
    build(iso, report, args)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, KeyError, AdapterError, ImportError) as error:
        print(f'isorespin: ERROR: {error}', file=sys.stderr)
        sys.exit(1)
