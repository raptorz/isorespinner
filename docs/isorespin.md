# isorespin：IA32 UEFI 适配

此实现基于旧版 `isorespin.sh` 8.7.1，不使用 `isorespinner.sh`。
目标硬件为 **64 位 x86 CPU + 32 位 UEFI**，不是 32 位操作系统。
IA32 引导使用未签名的 GRUB，需关闭 Secure Boot。

## 范围与验证状态

| 镜像 | 构建路径 | 当前验证程度 |
| --- | --- | --- |
| Mint 20.3 | 原有 8.7.1 流程、原有 EFI 和模块 | 保留原路径；嵌入引导资源逐字节回归检查；未重新安装实测 |
| Mint 22.3 Xfce | 单层 squashfs + Ubiquity 引导器阶段适配 | 原版 ISO 完整构建、离线依赖模拟、输出完整性及 EFI 结构检查通过；未完成实机启动/安装 |
| Mint 22.3 Cinnamon | 同上 | 识别及适配测试；尚未使用完整 Cinnamon ISO 构建、启动、安装 |
| Ubuntu / Xubuntu 26.04 Desktop | 原有多层 squashfs + Subiquity/curtin 适配 | 层结构、安装器和 EFI 重打包测试；尚未使用完整 26.04 ISO 构建、启动、安装 |

**新版路径目前是待实机验证的实现，不应把“生成成功”理解为硬件兼容认证。**
不保证老平板能满足新桌面和安装器的内存要求，也不保证显卡、无线、声音、
触摸屏、休眠等设备功能。

`--atom` 已移除：不再下载旧版 RTL8723 驱动、UCM/Broadcom 脚本，不注入旧硬件
参数。IA32 UEFI 支持仍默认启用。仓库中独立的历史驱动文件未删除。

## 使用

请保留仓库目录结构；现在脚本需要旁边的 `lib/isorespin/`。
构建主机使用 amd64 Linux、Python 3.10+。现代路径依赖：

```sh
sudo apt install python3 python3-yaml xorriso squashfs-tools mtools dpkg-dev ubuntu-keyring
```

先做无 sudo、无联网、无修改原镜像的内容检查：

```sh
./isorespin.sh --inspect -i ~/Downloads/iso/linuxmint-22.3-xfce-64bit.iso
```

生成镜像：

```sh
sudo ./isorespin.sh -i ~/Downloads/iso/linuxmint-22.3-xfce-64bit.iso
sudo ./isorespin.sh -i ~/Downloads/iso/linuxmint-22.3-cinnamon-64bit.iso
sudo ./isorespin.sh -i ~/Downloads/iso/ubuntu-26.04-desktop-amd64.iso
sudo ./isorespin.sh -i ~/Downloads/iso/xubuntu-26.04-desktop-amd64.iso
```

现代路径参数为 `-i/--iso`、`-w/--work-directory`、`--inspect`、`--keep-work`。
`-w` 指定工作和输出目录；默认当前目录，输出名为 `linuxium-原镜像名`。
已有输出不会被覆盖。默认要求至少 20 GiB 或输入 ISO 大小的五倍可用空间。
失败保留 `isorespin-build-*` 工作目录；成功默认清理本次临时目录。
需要联网下载目标发行版的 GRUB 包和依赖，安装时则可离线使用镜像内仓库。

仅给 `sudo xorriso` 免密权限不足以执行完整构建：squashfs 解包和重打包需要
保留 root 所有权、setuid、设备节点和 xattrs，因此构建器要求 root。
不通过 xorriso 的权限去获得其他命令的 root 权限。

现代路径不接受原脚本的换内核、升级系统、任意命令、持久化等高级选项，
也不使用旧版 GUI。暂不支持的参数会报错，不会悄悄忽略。
Mint 20.3 仍使用原有参数和构建流程；因该版本已结束支持，EOL 检查对 20.3
改为明确警告。这不会恢复其安全更新，也不会保证旧软件源永久可用。

## 实现与边界

- 通过 squashfs 内 `os-release` / `linuxmint/info` 判定版本、桌面和基础发行版，
  不根据 ISO 文件名或卷标中的 `Desktop` 字样判断。
- 保留原始内核、initramfs 和 Casper UUID，不升级整个 Live 系统。
  检查 `EFI_MIXED`、`EFI_STUB`、`EFI_HANDOVER_PROTOCOL`、`X86_64`。
- Live EFI 与 `i386-efi` 模块来自原脚本完整的 GRUB 2.04 资源包，不能混用
  新版模块。其是否能在实机加载新内核，仍须测试。该旧 GRUB 不是长期安全更新方案。
- 只解包含安装器的 squashfs 层，保留原层名、压缩算法、层级关系和安装源。
  没有在 Live 层安装/删除软件包，因此不伪造软件包 manifest 的版本。
- Ubiquity 在真正的引导器安装阶段调用公共 IA32 安装脚本；其他固件转交原脚本。
  不依赖某个 preseed 成功回调是否被加载。
- Subiquity 使用的 curtin 在安装依赖前准备离线仓库，并在 GRUB 阶段调用
  IA32 安装脚本。对代码结构的检查不匹配时停止，不盲目套用补丁。
- 修改过的安装器 snap 必须在 `seed.yaml` 中标为本地 **unasserted**，不再声称
  它符合发行版原签名。禁用这个定制安装器的自更新，防止更新覆盖 IA32 适配。
  不影响已安装系统中的普通软件更新。已预生成的 snap preseed 状态目前拒绝构建。
- 构建时 APT 使用隔离目录、目标发行版的软件源和 Ubuntu 签名密钥；从空状态
  解析依赖，再用镜像中的软件包状态补充升级所需依赖，不能让构建主机已安装的
  软件包导致漏包。构建过程不运行目标系统程序。
- 安装时只信任镜像内指定的本地仓库，不全局关闭软件源认证。脚本根据实际目标
  文件系统运行 `grub-install --target=i386-efi`，生成硬盘引导文件，而非复制 Live EFI。
  同时维护厂商入口和 `EFI/BOOT/BOOTIA32.EFI`；NVRAM 写入失败时保留回退入口。
- IA32 安装采用可共存的 `grub-efi-ia32-bin`，不强装与 `grub-pc` 冲突的
  `grub-efi-ia32` 主包，避免连带删除 Mint 的 shim、签名引导包及安装器。
  镜像内自建的 `isorespin-ia32` 维护包在首次安装完成后，通过 dpkg 触发器和
  内核更新钩子重新生成 IA32 EFI 文件；更新时 ESP 必须挂载。APT 安装禁止删除包。
  构建中额外使用镜像软件包状态和**仅本地仓库**模拟安装，检查离线依赖完整性。
- ISO 继承输入镜像的完整启动布局；更新真正的 EFI FAT 镜像/附加分区和 El Torito
  加载大小。输出后重新读取实际 EFI 分区验证 IA32 文件，而非只检查外层目录。
  目标是保持混合 ISO 的 `dd` 写盘能力，实机验收也应使用这种写盘方式。

构建报告写入输出旁的 `.iso.build.json` 和镜像内 `/isorespin/build.json`。
安装日志位于目标系统的 `/var/log/installer/isorespin-ia32.log`。

## 测试

```sh
bash -n isorespin.sh
bash -n lib/isorespin/install-ia32
bash -n lib/isorespin/refresh-ia32
python3 -m unittest discover -s tests -v
```

测试包含实际构造和重打包小型 ISO、嵌入/附加 EFI 分区读回、保留 x64 引导文件、
镜像内容识别、安装器分支和旧资源回归。它们不等同于固件启动测试。

### 2026-09-28 本地 Mint 22.3 Xfce 构建记录

- 21 项自动测试通过；完整构建生成约 2.9 GiB 的 ISO。
- 使用输出仓库和原镜像的 dpkg 状态模拟离线安装：新增 `grub-efi-ia32-bin`
  与 `isorespin-ia32`，零升级、零删除；离线仓库共 79 个软件包。
- 输出 ISO 中 1399 项 MD5 校验全部通过；内核、initramfs、软件包 manifest、
  dpkg 状态及 `sudo` 内容与原版一致，`sudo` 仍为 `root:root`、模式 `4755`。
- 实际 EFI FAT 分区中的 IA32 文件与旧版资源一致，x64 EFI 文件与原版一致；
  BIOS、UEFI、混合 MBR/GPT/APM 布局均保留。
- 未进行 IA32 固件虚拟机或平板的 Live、安装、重启及升级实测。

输入 `linuxmint-22.3-xfce-64bit.iso` 的 SHA256：

```text
45a835b5dddaf40e84d776549e0b19b3fbd49673b6cc6434ebddbfcd217df776
```

本次输出 `linuxium-linuxmint-22.3-xfce-64bit.iso` 的 SHA256：

```text
f94c11440cea55c4f2e0067b5b104c221fb195969f0409ee8939b9c335ad7581
```

每个目标镜像还需依次验证：IA32 UEFI 进入 Live → 应用/安装器正常 → 断网安装
→ 拔掉 U 盘后启动 → 更新内核和 GRUB 后再次启动。虚拟机必须使用 IA32 固件和
64 位 CPU，普通 x64 OVMF 不能替代；最终仍以平板实测为准。

参考：
[Ubuntu 26.04 文件清单](https://releases.ubuntu.com/26.04/ubuntu-26.04-desktop-amd64.list)、
[Xubuntu 26.04 文件清单](https://cdimage.ubuntu.com/xubuntu/releases/26.04/release/xubuntu-26.04-desktop-amd64.list)、
[curtin 源码](https://github.com/canonical/curtin/blob/main/curtin/commands/curthooks.py)、
[Subiquity 源码](https://github.com/canonical/subiquity)、
[Ubuntu IA32 GRUB 软件包](https://packages.ubuntu.com/resolute/grub-efi-ia32-bin)。
