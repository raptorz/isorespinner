"""Run with: python3 -m unittest discover -s tests -v (no sudo required)."""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'lib/isorespin'))
import adapters
import modern


CURTIN = '''
class DISTROS:
    debian = 'debian'

def install_missing_packages(cfg, target, osfamily=DISTROS.debian):
    """An upstream-shaped dependency-selection fixture."""
    needed_packages = set()
    installed_packages = set()
    uefi_pkgs = ['grub-efi-amd64', 'grub-efi-amd64-signed', 'shim-signed']
    needed_packages.update([pkg for pkg in uefi_pkgs
                            if pkg not in installed_packages])
    return needed_packages

def setup_grub(cfg, target, osfamily, variant):
    return 'original'
'''


class Profiles(unittest.TestCase):
    def test_mint_desktops_use_internal_metadata(self):
        for edition in ('Cinnamon', 'Xfce'):
            release = {'ID': 'linuxmint', 'VERSION_ID': '22.3', 'UBUNTU_CODENAME': 'noble', 'ISORESPIN_EDITION': edition}
            self.assertEqual(modern.profile('Linux Mint 22.3 "Zena" - Release amd64', release), 'mint22-ubiquity')
        release['ISORESPIN_EDITION'] = 'MATE'
        with self.assertRaises(RuntimeError):
            modern.profile('', release)

    def test_mint20_retains_legacy_path(self):
        self.assertEqual(modern.profile('Linux Mint 20.3', {'ID': 'linuxmint', 'VERSION_ID': '20.3'}), 'mint20-legacy')

    def test_ubuntu_and_xubuntu(self):
        for label in ('Ubuntu 26.04 LTS', 'Xubuntu 26.04', 'Xubuntu 26.04.1'):
            self.assertEqual(modern.profile(label, {'ID': 'ubuntu', 'VERSION_ID': '26.04'}), 'ubuntu26-subiquity')
        for label in ('Ubuntu-Server 26.04', 'Kubuntu 26.04'):
            with self.assertRaises(RuntimeError):
                modern.profile(label, {'ID': 'ubuntu', 'VERSION_ID': '26.04'})

    def test_os_release_is_data_not_shell(self):
        self.assertEqual(modern.os_release('ID="linuxmint"\nVALUE="$(false)"')['VALUE'], '$(false)')

    def test_kernel_capabilities(self):
        with self.assertRaises(RuntimeError):
            modern.check_kernel({'kernel_configs': {}})
        with self.assertRaises(RuntimeError):
            modern.check_kernel({'kernel_configs': {'boot/config': {'CONFIG_EFI_MIXED': False}}})
        modern.check_kernel({'kernel_configs': {'boot/config': {'CONFIG_EFI_MIXED': True}}})

    def test_lbas_and_paths_with_spaces(self):
        self.assertEqual(modern.parse_lbas("File data lba: 0 , 20 , 1 , 5 , '/a b'"), {'/a b': (40960, 5)})
        with self.assertRaises(RuntimeError):
            modern.parse_lbas("File data lba: 1 , 20 , 1 , 5 , '/a b'")


class InstallerAdapters(unittest.TestCase):
    def test_curthooks_ia32_and_other_firmware(self):
        namespace = {}
        source = adapters.patch_curthooks(CURTIN)
        ast.parse(source)
        exec(source, namespace)
        actions = []
        namespace['_isorespin_target'] = lambda action, target: actions.append((action, target))
        namespace['_isorespin_ia32'] = lambda: True
        self.assertEqual(namespace['install_missing_packages']({}, '/target'), {'grub-efi-ia32-bin', 'grub2-common', 'efibootmgr'})
        self.assertIsNone(namespace['setup_grub']({}, '/target', 'debian', 'ubuntu'))
        self.assertEqual(actions, [('prepare', '/target'), ('install', '/target')])
        namespace['_isorespin_ia32'] = lambda: False
        self.assertIn('shim-signed', namespace['install_missing_packages']({}, '/target'))
        self.assertEqual(namespace['setup_grub']({}, '/target', 'debian', 'ubuntu'), 'original')
        self.assertEqual(len(actions), 2)

    def test_changed_upstream_is_rejected(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.patch_curthooks(CURTIN.replace('uefi_pkgs', 'renamed_upstream'))
        with self.assertRaises(adapters.AdapterError):
            adapters.patch_curthooks(adapters.patch_curthooks(CURTIN))

    def test_direct_grub_install_target(self):
        namespace = {}
        exec(adapters.patch_install_grub("def get_grub_package_name(target_arch, uefi):\n    return ('original', 'x86_64-efi')\n"), namespace)
        namespace['_isorespin_ia32'] = lambda: True
        self.assertEqual(namespace['get_grub_package_name']('amd64', True), ('grub-efi-ia32-bin', 'i386-efi'))
        self.assertEqual(namespace['get_grub_package_name']('amd64', False)[0], 'original')

    def test_seed_keeps_other_snaps_asserted(self):
        seed = {'snaps': [{'name': 'ubuntu-desktop-bootstrap', 'file': 'installer.snap', 'id': 'signed-id', 'revision': 123},
                          {'name': 'firefox', 'file': 'firefox.snap', 'id': 'firefox-id'}]}
        adapted = adapters.unassert_seed(seed, {'installer.snap'})
        self.assertTrue(adapted['snaps'][0]['unasserted'])
        self.assertNotIn('id', adapted['snaps'][0])
        self.assertNotIn('unasserted', adapted['snaps'][1])
        with self.assertRaises(adapters.AdapterError):
            adapters.unassert_seed(seed, {'missing.snap'})

    def test_wrapper_passes_through_arguments(self):
        wrapper = adapters.ubiquity_wrapper()
        self.assertIn('exec /usr/share/grub-installer/grub-installer.isorespin-original "$@"', wrapper)
        self.assertIn('exec /usr/lib/isorespin/install-ia32 install "$1"', wrapper)
        self.assertNotIn('success_command', wrapper)
        self.assertEqual(subprocess.run(['sh', '-n'], input=wrapper.encode()).returncode, 0)

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg tools not installed')
    def test_maintenance_package_is_additive_and_has_update_triggers(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            destination = work / 'repo'
            destination.mkdir()
            package = modern.maintenance_package(work, destination)
            deps = subprocess.check_output(['dpkg-deb', '-f', package, 'Depends']).decode()
            self.assertIn('grub-efi-ia32-bin', deps)
            self.assertNotIn('grub-efi-ia32,', deps)
            root = work / 'ia32-maintenance-package'
            self.assertIn('interest-noawait /usr/lib/grub', (root / 'DEBIAN/triggers').read_text())
            self.assertTrue((root / 'etc/kernel/postinst.d/zz-isorespin-ia32').is_file())
            for path in (root / 'DEBIAN/postinst', root / 'usr/lib/isorespin/refresh-ia32',
                         modern.LIB / 'install-ia32'):
                self.assertEqual(subprocess.run(['bash', '-n', path]).returncode, 0)
            refresh = (root / 'usr/lib/isorespin/refresh-ia32').read_text()
            self.assertLess(refresh.index('[[ ! -f $marker ]]'), refresh.index('grub-install --target='))
            install = (modern.LIB / 'install-ia32').read_text()
            self.assertIn('--no-remove -y install isorespin-ia32', install)
            self.assertNotIn('--allow-remove-essential', install)


class LegacyRegression(unittest.TestCase):
    def test_payload_identical_to_original(self):
        baseline = subprocess.check_output(['git', 'show', '4b6309e:isorespin.sh'], cwd=REPO)
        original = baseline.split(b'\nexit 0\n', 1)[1]
        current = modern.SCRIPT.read_bytes().split(b'\nexit 0\n', 1)[1]
        self.assertEqual(hashlib.sha256(current).digest(), hashlib.sha256(original).digest())

    def test_embedded_kit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            modern.legacy_bootkit(path)
            self.assertTrue(modern.pe_ia32(path / 'efi_bootia32.efi'))
            self.assertEqual((path / 'efi_bootia32.efi').stat().st_size, 958464)
            self.assertTrue((path / 'grub/i386-efi/normal.mod').is_file())

    def test_atom_rejected_without_sudo(self):
        result = subprocess.run(['bash', str(modern.SCRIPT), '--atom'], capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'has been removed', result.stderr)
        self.assertNotIn(b'sudo:', result.stderr)
        self.assertNotIn('ATOM_WIFI_PACKAGE', modern.SCRIPT.read_text())

    def test_help_without_sudo(self):
        result = subprocess.run(['bash', str(modern.SCRIPT), '--help'], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'--inspect', result.stdout)
        self.assertFalse(result.stderr)


@unittest.skipUnless(all(shutil.which(c) for c in ('xorriso', 'mformat', 'mcopy', 'mmd', 'mksquashfs', 'unsquashfs')), 'ISO tools not installed')
class ImageIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='isorespin-test-')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.tree = self.work / 'tree'
        self.tree.mkdir()
        self.kit = self.work / 'kit'
        self.kit.mkdir()
        modern.legacy_bootkit(self.kit)
        modern.write(self.tree / '.disk/info', 'Linux Mint 22.3 "Zena" - Release amd64\n')
        modern.write(self.tree / 'boot/grub/grub.cfg', 'set timeout=5\n')
        modern.write(self.tree / 'EFI/BOOT/bootx64.efi', 'fixture-x64-not-executable')
        self.esp = self.work / 'efi.img'
        with self.esp.open('wb') as stream:
            stream.truncate(4 * 1024 * 1024)
        modern.run(['mformat', '-i', self.esp, '::'])
        for name in ('::/EFI', '::/EFI/BOOT'):
            modern.run(['mmd', '-i', self.esp, name])
        modern.run(['mcopy', '-i', self.esp, self.tree / 'EFI/BOOT/bootx64.efi', '::/EFI/BOOT/BOOTX64.EFI'])

    def replay(self, appended):
        source = self.work / 'input.iso'
        if appended:
            args = ['-append_partition', '2', '0xef', self.esp, '-appended_part_as_gpt',
                    '-e', '--interval:appended_partition_2:all::', '-no-emul-boot']
        else:
            shutil.copy2(self.esp, self.tree / 'boot/grub/efi.img')
            args = ['-e', 'boot/grub/efi.img', '-no-emul-boot', '-efi-boot-part', '--efi-boot-image']
        modern.run(['xorriso', '-as', 'mkisofs', '-R', '-o', source, *args, self.tree])
        iso = modern.ISO(source)
        layout = modern.boot_layout(iso)
        self.assertEqual(layout['appended'], 2 if appended else None)
        output_tree = self.work / 'output-tree'
        modern.run(['xorriso', '-osirrox', 'on', '-indev', source, '-extract', '/', output_tree])
        # No root needed for ISO/FAT work. Production squashfs editing still requires it.
        bootwork = self.work / 'bootwork'
        bootwork.mkdir()
        esp = modern.add_bootkit(iso, output_tree, bootwork, layout)
        output = self.work / 'output.iso'
        modern.run(modern.replay_command(iso, output_tree, output, layout, esp))
        rewritten = modern.ISO(output)
        rewritten_layout = modern.boot_layout(rewritten)
        with output.open('rb') as stream:
            stream.seek(rewritten_layout['offset'])
            fat = stream.read(rewritten_layout['size'])
        output_esp = self.work / 'output-esp.img'
        output_esp.write_bytes(fat)
        efi = self.work / 'output-ia32.efi'
        x64 = self.work / 'output-x64.efi'
        modern.run(['mcopy', '-i', output_esp, '::/EFI/BOOT/BOOTIA32.EFI', efi])
        modern.run(['mcopy', '-i', output_esp, '::/EFI/BOOT/BOOTX64.EFI', x64])
        self.assertTrue(modern.pe_ia32(efi))
        self.assertEqual(x64.read_text(), 'fixture-x64-not-executable')
        self.assertEqual(efi.read_bytes(), (self.kit / 'grub_bootia32.efi').read_bytes())

    def test_embedded_efi_replay(self):
        self.replay(appended=False)

    def test_appended_efi_replay(self):
        self.replay(appended=True)

    def test_mint_probe_from_squashfs(self):
        root = self.work / 'root'
        modern.write(root / 'usr/lib/os-release', 'ID=linuxmint\nVERSION_ID=22.3\nUBUNTU_CODENAME=noble\n')
        modern.write(root / 'etc/linuxmint/info', 'EDITION="Cinnamon"\n')
        modern.write(root / 'usr/share/grub-installer/grub-installer', '#!/bin/sh\n', 0o755)
        flags = ('X86_64', 'EFI_MIXED', 'EFI_STUB', 'EFI_HANDOVER_PROTOCOL')
        modern.write(root / 'boot/config-6.14', ''.join(f'CONFIG_{f}=y\n' for f in flags))
        (self.tree / 'casper').mkdir()
        squashfs = self.tree / 'casper/filesystem.squashfs'
        modern.run(['mksquashfs', root, squashfs, '-noappend', '-all-root', '-processors', '1', '-no-progress'])
        iso_path = self.work / 'probe.iso'
        modern.run(['xorriso', '-as', 'mkisofs', '-R', '-o', iso_path, self.tree])
        report = modern.inspect(modern.ISO(iso_path))
        self.assertEqual(report['profile'], 'mint22-ubiquity')
        self.assertEqual(report['release']['ISORESPIN_EDITION'], 'Cinnamon')
        modern.check_kernel(report)

    def test_ubuntu_layered_installer(self):
        import yaml
        modern.write(self.tree / '.disk/info', 'Xubuntu 26.04 - Release amd64\n')
        roots = {}
        casper = self.tree / 'casper'
        casper.mkdir()
        for layer in ('minimal', 'minimal.standard', 'minimal.standard.live'):
            roots[layer] = self.work / layer
            roots[layer].mkdir()
        base = roots['minimal']
        live = roots['minimal.standard.live']
        modern.write(base / 'usr/lib/os-release', 'ID=ubuntu\nVERSION_ID=26.04\nVERSION_CODENAME=resolute\n')
        modern.write(base / 'boot/config-7.0', ''.join(f'CONFIG_{f}=y\n' for f in
                     ('X86_64', 'EFI_MIXED', 'EFI_STUB', 'EFI_HANDOVER_PROTOCOL')))
        modern.write(roots['minimal.standard'] / 'etc/desktop-marker', 'must be unchanged\n')
        snap_root = self.work / 'snap-fixture'
        modern.write(snap_root / 'meta/snap.yaml', 'name: ubuntu-desktop-bootstrap\nconfinement: classic\n')
        commands = snap_root / 'lib/python3.12/site-packages/curtin/commands'
        modern.write(commands / 'curthooks.py', CURTIN)
        modern.write(commands / '__pycache__/curthooks.cpython-312.pyc', 'old-bytecode')
        modern.write(commands / 'install_grub.py', "def get_grub_package_name(target_arch, uefi):\n    return ('original', 'x86_64-efi')\n")
        modern.write(snap_root / 'lib/python3.12/site-packages/subiquity/server/controllers/refresh.py',
                     'class RefreshController:\n    async def check_for_update(self, context):\n        pass\n'
                     '    async def start_update(self, context):\n        pass\n')
        seed = live / 'var/lib/snapd/seed'
        (seed / 'snaps').mkdir(parents=True)
        snap_name = 'ubuntu-desktop-bootstrap_589.snap'
        modern.run(['mksquashfs', snap_root, seed / 'snaps' / snap_name, '-noappend', '-processors', '1', '-no-progress'])
        modern.write(seed / 'seed.yaml', yaml.safe_dump({'snaps': [{'name': 'ubuntu-desktop-bootstrap', 'file': snap_name, 'classic': True}]}))
        for name, root in roots.items():
            modern.run(['mksquashfs', root, casper / (name + '.squashfs'), '-noappend', '-processors', '1', '-no-progress'])
            modern.write(casper / (name + '.size'), '10000\n')
        catalog = {'version': 1, 'sources': [{'id': 'xubuntu-desktop', 'path': 'minimal.standard.squashfs', 'size': 20000, 'type': 'fsimage-layered'}]}
        modern.write(casper / 'install-sources.yaml', yaml.safe_dump(catalog))
        iso_path = self.work / 'layers.iso'
        modern.run(['xorriso', '-as', 'mkisofs', '-R', '-o', iso_path, self.tree])
        iso = modern.ISO(iso_path)
        report = modern.inspect(iso)
        self.assertEqual(report['profile'], 'ubuntu26-subiquity')
        hashes = {name: modern.sha(casper / (name + '.squashfs')) for name in ('minimal', 'minimal.standard')}
        change_work = self.work / 'changes'
        change_work.mkdir()
        changes = modern.adapt_layers(iso, report, self.tree, change_work)
        self.assertEqual([c['layer'] for c in changes], ['/casper/minimal.standard.live.squashfs'])
        for name, before in hashes.items():
            self.assertEqual(before, modern.sha(casper / (name + '.squashfs')))
        self.assertEqual(yaml.safe_load((casper / 'install-sources.yaml').read_text()), catalog)
        new_seed = modern.run(['unsquashfs', '-cat', casper / 'minimal.standard.live.squashfs', 'var/lib/snapd/seed/seed.yaml']).stdout
        self.assertTrue(yaml.safe_load(new_seed)['snaps'][0]['unasserted'])

    def test_mint_isohybrid_bios_gpt_apm_replay(self):
        # Optional real-media regression: only boot records/binary are read from
        # this input. The rest of the test ISO is synthetic and tiny.
        mint = Path('/home/raptor/Downloads/iso/linuxmint-22.3-xfce-64bit.iso')
        if not mint.is_file():
            self.skipTest('Local Mint ISO not present')
        original = modern.ISO(mint)
        isolinux = self.tree / 'isolinux/isolinux.bin'
        isolinux.parent.mkdir()
        isolinux.write_bytes(original.read('/isolinux/isolinux.bin'))
        shutil.copy2(self.esp, self.tree / 'boot/grub/efi.img')
        source = self.work / 'hybrid.iso'
        modern.run(['xorriso', '-as', 'mkisofs', '-R', '-o', source,
                    '-isohybrid-mbr', f'--interval:local_fs:0s-15s:zero_mbrpt,zero_gpt,zero_apm:{mint}',
                    '-partition_offset', '16', '-apm-block-size', '2048',
                    '-b', 'isolinux/isolinux.bin', '-c', 'isolinux/boot.cat', '-no-emul-boot',
                    '-boot-load-size', '4', '-boot-info-table', '-eltorito-alt-boot',
                    '-e', 'boot/grub/efi.img', '-no-emul-boot', '-isohybrid-gpt-basdat', '-isohybrid-apm-hfsplus', self.tree])
        iso = modern.ISO(source)
        layout = modern.boot_layout(iso)
        tree = self.work / 'hybrid-tree'
        modern.run(['xorriso', '-osirrox', 'on', '-indev', source, '-extract', '/', tree])
        change_work = self.work / 'hybrid-work'
        change_work.mkdir()
        esp = modern.add_bootkit(iso, tree, change_work, layout)
        output = self.work / 'hybrid-output.iso'
        modern.run(modern.replay_command(iso, tree, output, layout, esp))
        report = modern.run(['xorriso', '-indev', output, '-report_el_torito', 'plain', '-report_system_area', 'plain']).stdout.decode()
        self.assertIn('BIOS', report)
        self.assertIn('GPT APM', report)
        self.assertEqual(modern.boot_layout(modern.ISO(output))['size'], esp.stat().st_size)


if __name__ == '__main__':
    unittest.main()
