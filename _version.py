# -*- coding: utf-8 -*-
"""版本号 (由 CI 在打包前覆盖).

本地开发直接跑 py 时 VERSION = "dev".
CI 打包 exe 时会用 `echo VERSION = "${{ github.ref_name }}"` 覆盖成
真实 tag, 比如 "v0.4.30". 这样从 GUI 的 "关于" 页 / exe --version 都能
一眼看出跑的是哪一版, 跟 git tag 对应.
"""

VERSION = "dev"
