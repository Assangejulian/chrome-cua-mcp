# 任务书：Chrome 视觉识别与快速鼠标操作模型（Chrome GUI Pilot）

> **版本**：v1.1（评审修订版）  
> **日期**：2026-11-16  
> **一期范围**：Google Chrome 浏览器（原生浏览器外壳 + 网页视口）

---

## 一、项目目标、边界与硬性约束

### 1.1 业务目标与一期范围（彻底对齐动作集）
构建一个**纯视觉驱动的 Chrome 操作智能体**：接收截屏与用户自然语言指令，在本地完成界面识别、动作决策并驱动真实键鼠。

为避免“例举了多步输入搜索却缺少对应动作定义”的口径撕裂，**一期明确包含以下两层递进范围，动作空间一次性定义完整**：
* **Phase 1A（核心先攻）**：**单步屏幕定位与鼠标/滚轮操作**（给定指令，直接点中目标按钮/链接/标签页或执行页面滚动）。
* **Phase 1B（闭环任务）**：**在 1A 基础上加入文本输入、快捷键与状态控制**，由外层 Python 调度器每步重新截图驱动模型连续决策，从而完整支持如“在搜索框输入中文关键词并回车/点搜索”等短链任务。

#### 一期封闭动作空间（所有动作均须可解析、可执行）
| 动作类型 | JSON 输出格式示例 | 字段与单位说明 |
| :--- | :--- | :--- |
| `left_click` | `{"action": "left_click", "coordinate": [520, 310]}` | 左键单击；坐标为 `[0, 1000]` 整数归一化坐标 |
| `double_click` | `{"action": "double_click", "coordinate": [520, 310]}` | 左键双击 |
| `right_click` | `{"action": "right_click", "coordinate": [520, 310]}` | 右键单击 |
| `move` | `{"action": "move", "coordinate": [520, 310]}` | 鼠标悬停（触发下拉菜单等） |
| `scroll` | `{"action": "scroll", "coordinate": [500, 500], "direction": "down", "amount": 3}` | 滚轮滚动：`coordinate` 为滚动落点（先移到目标容器内），`direction` 取 `up\|down\|left\|right`，`amount` 为滚轮**格数（clicks）**，Windows 下每格对应 `WHEEL_DELTA=120` |
| `type` | `{"action": "type", "text": "显卡天梯图"}` | 文本输入（默认先通过前一步 `left_click` 聚焦输入框，或外层支持可选附带坐标）。**中文输入强制方案**：禁止仅用英文按键模拟，Windows 必须采用**剪贴板写入（备份原剪贴板 → 写入文本 → `Ctrl+V` → 延时恢复原剪贴板）**或 Win32 `SendInput` `KEYEVENTF_UNICODE` 注入 |
| `hotkey` | `{"action": "hotkey", "keys": ["ctrl", "t"]}` | 组合键 / 功能键，如 `["ctrl","t"]`、`["ctrl","l"]`、`["enter"]`、`["esc"]` |
| `wait` | `{"action": "wait", "seconds": 1.0}` | 页面加载等待（不触发鼠标动作，等待后重新截屏） |
| `done` | `{"action": "done"}` | 任务已达成，正常结束闭环 |
| `not_found` | `{"action": "not_found", "reason": "目标不存在或被弹窗遮挡"}` | 拒绝盲点，触发外层异常处理/重试/人工接管 |

### 1.2 硬件约束与实测指标口径
| 维度 | 实测约束 / 指标目标 | 关键说明 |
| :--- | :--- | :--- |
| **本地推理硬件** | Windows + **NVIDIA GeForce RTX 4060 Laptop GPU（8188 MiB）** | 非桌面版 4060；Windows DWM 与多标签 Chrome 硬件加速会占用约 0.8–1.5 GiB，要求模型常驻与峰值显存在真实共存条件下不溢流至系统共享内存 |
| **延迟目标** | **预热后 P95 `T_step ≤ 3.0s`**（目标 P50 `≤ 1.8s`） | **保留 3 秒端到端门槛**：`T_step` 计时起点为截屏开始，终点为预处理、模型请求、JSON 校验及键鼠动作发送完成（**不含网页加载耗时与动作后的页面验证**）。另单列 `T_infer`（截屏完成 → JSON 解析完成）用于诊断，冷启动单独统计。若 M0/M4 实测在 4060 Laptop 上无法稳定达标，将显式与用户讨论目标调整，不私自放宽标准 |
| **训练算力预算** | Google Colab A100，共 **200 CCU** | 官方 FAQ 明确 A100 按实际动态倍率计费且不承诺固定可用小时数；必须在 M2 实测当前账号的实际 `CCU/h` 后再倒排步数 |

---

## 二、核心架构、闭环设计与坐标链路规范

### 2.1 底座选型：`Qwen/Qwen3.5-2B`
1. **不选 `Qwen3.8-27B` / 暂缓 `4B` 的原因**：Qwen3.8 官方无 2B 级原生多模态小模型；27B 无法放进 4060 Laptop 8188 MiB 显存；而 4B 在开启多标签 Chrome 共存时接近 8 GiB 边界，极易溢流共享内存导致延迟雪崩。
2. **关于 `Qwen/Qwen3.5-2B` 的准确认知**：
   * 它是原生早融合多模态模型（~306M SigLIP-2 风格 ViT + Gated DeltaNet 混合注意力骨干），按 **32 像素**网格对齐图像 token。
   * 官方 **ScreenSpot-Pro 成绩 54.5 本身就是非思考（non-thinking）默认模式下的评测值**；也就是说模型**默认即非思考**，我们只需要在训练模板与推理调用中保持不开启 thinking，并约束其不输出冗长解释，而非“从思考强行改成非思考”。
   * **注意**：ScreenSpot-Pro 分值不等于真实 Chrome 任务成功率（尤其是中文网页与 Chrome 外壳），真实能力必须以独立自采 Chrome 测试集为准。

### 2.2 精确坐标与 DPI 链路（杜绝错位与越界）
Windows 桌面（100%/125%/150% DPI 缩放、Win10/11 不可见阴影、多显示器负坐标）是 GUI 点击最大的工程坑，必须严格统一如下：

1. **DPI 感知统一**：采集端与推理执行端 Python 进程启动第一时间调用 `SetProcessDpiAwarenessContext(-4)`（`PER_MONITOR_AWARE_V2`，降级 `SetProcessDpiAwareness(2)`），确保 `mss`、Win32 窗口矩形与 `SendInput` 全程在同一套**物理像素**下工作。
2. **ROI 区域明确定义（禁止混用三套矩形）**：
   * 区分三个概念：**屏幕全局坐标**、`GetWindowRect`（包含 Windows ~7px 不可见阴影）、**`DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`**（真实可见外框，含 Chrome 标签栏与地址栏）、以及 `GetClientRect + ClientToScreen`（客户区）。
   * **本项目一期统一口径**：在 M0 先用实机截屏确认 Chrome 无边框自绘标签栏落在哪一区域，优先统一采用 **`DWMWA_EXTENDED_FRAME_BOUNDS`（真实可见窗口矩形，涵盖标签栏 + 地址栏 + 书签栏 + 网页视口）**，**采集、训练、推理三阶段必须完全一致**，禁止用 `GetWindowRect` 带阴影混裁。
3. **多显示器与负坐标兼容**：副屏在主屏左侧/上方时 `roi_left` 或 `roi_top` 为负数，不得做 `max(..., 0)` 截断。
4. **0–1000 还原与边界 Clamp（防 `1000` 落出窗口外）**：
   ```python
   # Clamp normalized [0, 1000] coordinates and map to ROI pixels [0, w-1] / [0, h-1]
   rel_x = min(max(round(x_norm / 1000.0 * (roi_w - 1)), 0), roi_w - 1)
   rel_y = min(max(round(y_norm / 1000.0 * (roi_h - 1)), 0), roi_h - 1)
   screen_x = roi_left + rel_x
   screen_y = roi_top + rel_y
   ```
5. **误差排查顺序**：测试中若出现小图标点击偏离，**先做工程诊断**（把还原点叠加画回送入模型的实际缩放图上检查：是否为 DPI 错位、阴影偏移或 `IMAGE_MAX_TOKEN_NUM` 缩图太糊导致图标不可辨），排除工程误差后再判断是否需要调高图像 token 上限或加训数据。

### 2.3 执行闭环与安全护栏（1A/1B 共用）
点击发出不等于任务完成，完整运行闭环必须包含：
1. **步前窗口锁校验与观测失效处理**：真正发出键鼠前，重新检查目标 Chrome `hwnd` 是否存在、未最小化、处于前台，且当前窗口 `(left, top, width, height)` 与本步截屏时刻一致；**窗口移动、尺寸变化或失焦时，立刻作废旧坐标并重截**。同时需注意，窗口几何未变并不能覆盖页面自身在推理期间的异步变化（如延迟弹窗遮挡、动态广告位移）；若页面局部在关键动作前后发生非预期突变，或前一步动作触发了未预期的页面跳转，外层应判为**观测失效**并重新截屏后再决策，禁止沿用过时帧坐标。
2. **动作后画面变化监测与语义成功条件**：
   * 动作执行后等待短缓冲（如 `150–300ms`），重新截取 Chrome 窗口；**画面差分 / 感知哈希仅用于检测界面是否发生变化及辅助识别死循环卡死（例如最近 3–5 步连续对同一区域重复操作且画面无变化），绝不能替代任务完成判断**。
   * **语义成功条件**：任务是否真正达成，必须由下一轮视觉状态语义确认（如搜索任务需在界面中看到搜索结果页或预期查询关键词，切换标签需看到目标标签成为活动标签页，并由模型或外层校验确认后返回 `done`）。若连续无变化卡死，触发小幅 `scroll`、重采样或终止并报人工接管。
3. **步数与重试上限 + 急停开关（Kill Switch）**：
   * JSON 解析失败或坐标越界：**严禁执行鼠标动作**，最多允许同图重采样 `1` 次，连续失败 `2` 次中止任务。
   * 单任务设硬性步数上限（如 `max_steps = 15`）。
   * 全局急停入口：`Ctrl+Alt+Q` 热键 + 屏幕四角 `FailSafe` 钩子，随时一键切断控制权。

### 2.4 速度与缓存实操说明
* **前缀缓存（Prefix Caching）的真实边界**：固定 System Prompt 可由推理引擎（`llama.cpp` prompt cache 或 WSL2 `vLLM` APC）复用文本前缀 KV，**但新截图每次都会变，ViT 视觉编码和图像 token prefill 仍是主要计算耗时，不会因开启前缀缓存而消失**。一切加速收益须在 M0 / M5 实机测量。
* **本地端到端优化**：推理服务常驻显存；保证与推理后端同一套像素上限（如 `IMAGE_MAX_TOKEN_NUM=1024` 约等于 105 万像素 / `1280×800` 级别）。

---

## 三、全链路技术栈（候选配置，待 M2 实机验证后锁版）

| 环节 | 候选技术选型与版本参考 | 说明 |
| :--- | :--- | :--- |
| **基座模型** | `Qwen/Qwen3.5-2B` | 默认 non-thinking；如后续出现适配问题备选 `Qwen/Qwen3-VL-2B-Instruct` |
| **采集与控制（Windows）** | Python 3.10–3.12 + `mss` + `pynput` + `pywin32` + `pyperclip` | 物理像素截屏、按下瞬时抓帧、Win32 窗口校验与 Unicode/剪贴板中文输入 |
| **训练环境（Colab A100）** | 候选依赖参考 `ms-swift` v4.3 `Qwen3_5-Best-Practice`：<br>`ms-swift>=4.3`, `transformers>=5.9`, `qwen_vl_utils>=0.0.14`, `peft`, `liger-kernel`, `torchao>=0.16.0`, `flash-linear-attention>=0.4.2`, `causal-conv1d`, `flash-attn==2.8.3` | **当前尚未完成全流程试跑**；M2 已观察到 `torch==2.14.1+cu130`、`ms_swift==4.6.1`、`transformers==5.22.2`、`peft==0.20.0`、`fla==0.6.0` 与源码编译 `causal_conv1d`，且因 Colab 预装 `torchao==0.10.0` 与 `peft==0.20.0` 冲突已补升 `torchao>=0.16.0`；待 M2 全部跑通后经 `pip freeze` 正式锁版 |
| **本地推理引擎（Windows 4060）** | 原生 Windows：**`llama.cpp`（`llama-server.exe` + CUDA，加载合并导出的 `GGUF` + 配套新 `mmproj`）** 或 **`Ollama`**；WSL2 备选：**`vLLM>=0.17.0`** | 在 M2 与 M5 用同一验证集横向对比原版 HF / 合并 HF / GGUF Q8_0 / GGUF Q4_K_M |

---

## 四、数据采集时序、bbox 来源与分层配比

### 4.1 采集时序修正：只保存“点击前稳定帧”
监听到点击后再截屏往往已拍到网页跳转后的结果。正确采集架构：
* 后台 `mss` 线程以低开销按 ~8–10 FPS 维持最近 1 秒的环形内存帧队列（每帧绑定 `timestamp` 与 Chrome `DWM` 窗口矩形）。
* 当 `pynput` 捕获 **`on_click(pressed=True)`（鼠标按下瞬间）** 时，回溯取 **`t_click - 80ms`** 左右的稳定前帧（既不是跳转后，也避开按下瞬间控件高亮闪烁）；`type` / `hotkey` 同样在首次键入前抓帧。

### 4.2 `bbox` 获取、训练样本样例与元数据关联规范
**只有点击点 `(x, y)` 无法计算 Point-in-Box 指标，也无法做框内奖励 GRPO**。`bbox` 按以下优先级获取（**纯视觉边界声明：以下 CDP 与 UIA 工具仅用于 M1 离线采图标注和 M4 离线自动评测，正式上线推理与执行仍只接收 Chrome 截图和自然语言指令，不读取 DOM 或 UIA**）：
1. **网页内元素（离线自动框）**：录制端通过 Chrome DevTools Protocol (CDP) / 轻量辅助脚本在点击坐标处调用 `document.elementFromPoint` → `getBoundingClientRect()`（叠加视口偏置），自动记录 `bbox`。
2. **Chrome 原生外壳（标签/地址栏/工具栏）**：通过 Windows `UI Automation (uiautomation / pywinauto)` 查询对应坐标命中的原生控件 `BoundingRectangle`；UIA 拿不到的自绘元素用轻量矩形标注工具补齐。
3. **人工目检**：训练抽样检查（不少于 100 条），**私有验证集与私有测试集 100% 人工复核 `bbox` 贴合度**。

#### 1）喂给 `ms-swift` 的训练记录样例（`messages` / `images`）
训练文件（如 `train_chrome_mix.jsonl`）中的每条样本采用标准多模态对话格式；对于 `wait` / `done` / `not_found` / `type` / `hotkey` 等非定位动作，**`coordinate` 与 `bbox` 允许省略或置为 `null`**，不强求所有动作都绑定真实定位框：

```json
{
  "sample_id": "chrome_20261116_s01_00012",
  "messages": [
    {
      "role": "system",
      "content": "你是Chrome浏览器GUI操作助手。根据截图和用户指令，直接输出JSON格式的单步动作，不要输出任何多余解释。"
    },
    {
      "role": "user",
      "content": "<image>点击新建标签页加号按钮"
    },
    {
      "role": "assistant",
      "content": "{\"action\": \"left_click\", \"coordinate\": [268, 19]}"
    }
  ],
  "images": ["data/chrome_frames/s01_00012.png"]
}
```

非定位动作训练记录示例（无需坐标与 `bbox`）：
```json
{
  "sample_id": "chrome_20261116_s01_00045",
  "messages": [
    {
      "role": "system",
      "content": "你是Chrome浏览器GUI操作助手。根据截图和用户指令，直接输出JSON格式的单步动作，不要输出任何多余解释。"
    },
    {
      "role": "user",
      "content": "<image>点击右上角不存在的“导出PDF”按钮"
    },
    {
      "role": "assistant",
      "content": "{\"action\": \"not_found\", \"reason\": \"目标控件未在当前画面出现\"}"
    }
  ],
  "images": ["data/chrome_frames/s01_00045.png"]
}
```

#### 2）评测与溯源元数据表（通过 `sample_id` 关联，不泄漏进训练 Prompt）
所有评测和溯源字段存放在伴随索引文件（如 `metadata_chrome.jsonl`）中，**通过 `sample_id` 与训练 `messages` 记录一一对应**（训练导出时剥离 `bbox` 等评测真值字段，仅在验证集 `Point-in-Box` 计分、分桶报告及可选 GRPO 奖励计算时按 `sample_id` 关联回查）：
```json
{
  "sample_id": "chrome_20261116_s01_00012",
  "image_path": "data/chrome_frames/s01_00012.png",
  "instruction": "点击新建标签页加号按钮",
  "action": "left_click",
  "coordinate": [268, 19],
  "bbox": [258, 10, 278, 28],
  "source": "chrome_shell",
  "site_domain": "chrome://newtab",
  "session_id": "sess_20261116_01",
  "crop_mode": "dwm_chrome",
  "window_rect": [0, 0, 1920, 1040],
  "dpi_scale": 1.25,
  "img_w": 1920,
  "img_h": 1040
}
```
> 注：若该条为 `wait`、`done`、`not_found`、`type` 或 `hotkey`，伴随元数据中的 `coordinate` 和 `bbox` 字段可设为 `null` 或省略。

### 4.3 开源数据集质检 & 负样本/难例设计
* **开源数据源（Hugging Face）**：`showlab/ShowUI-web`、`OS-Copilot/OS-Atlas-data`（Web 子集）、`osunlp/Multimodal-Mind2Web`。
* **入库前硬检查**：
  1. 校验图片字节完整可解码、无损坏路径；
  2. 统一校准坐标格式（识别绝对像素、`[0,1]` 浮点或 `[0,1000]` 整数），过滤非法的框外点击点；
  3. **剔除整页长图（Full-page scrolling screenshots）**：真实运行只截当前窗口，长图极端纵横比会污染 2B 的空间位置先验。
* **必须混入的 5%–10% 对抗与边界样本**：
  * 目标不存在（要求输出 `not_found`）；
  * 被弹窗/Cookie 条遮挡（优先点关闭或提示遮挡）；
  * 列表/表格中多个同类按钮（如第 2 行的“编辑”与第 4 行的“编辑”，强化相对位置语义）。

### 4.4 严格划分与采样配比（防稀释、防泄漏）
* **防泄漏切分**：严格按 **`site_domain + session_id` 分组切分 `train / val / test`**。绝不允许同一个浏览会话的相邻帧或同一张截图的不同问题跨集分布；**`test` 集全程锁死只读，绝不回流到后续二轮错题重训**。
* **防稀释配比**：1.5w–2.5w 条通用英文网页数据很容易把 500–1,500 条 Chrome 自采数据“淹没”。采用**加权采样或两阶段课程**：
  * **推荐做法**：单个训练配置内，通过数据集采样权重/适度过采样（注意单条表述多样化以防死背），使 **Chrome 原生外壳 + 中文网页自采样本占有效训练样本的 20%–35%**；或先用通用 Web 跑 1 epoch，再混合 Chrome 专项 + 20% 通用回放跑 1 epoch。

---

## 五、分阶段执行计划与 200 CCU 预算管理

### 5.1 Colab 高效存储与 I/O 铁律
* **严禁直接从 Google Drive 读取几万张散装图片训练**（小文件随机 I/O 会拖慢 GPU 到个位数利用率，纯烧 CCU）。
* **正确做法**：
  1. 用免费 CPU 实例或本地电脑将所有图片打包为 `.tar` / `.zip` 存入 Drive（只做持久备份）；
  2. 切到 A100 后，第一时间把模型缓存与数据压缩包拷贝到 **Colab 本地高速盘 `/content/`** 并解压读取；
  3. Checkpoint 先完整写入 `/content/outputs/`，再定时/结束时增量同步回 Google Drive。

### 5.2 阶段里程碑（保留科学顺序）
* **M0：本机 RTX 4060 Laptop 基线测试（0 CCU）**
  * 本地跑通原版 `Qwen/Qwen3.5-2B`（GGUF Q8_0 / Q4_K_M + mmproj），跑通 `DWM` 窗口裁剪、DPI 映射、边界 clamp 与中文输入链路；
  * 实测原版预热后 **`T_step`（P50 / P95，对标 `≤ 3.0s` 门槛）**、分段诊断 `T_infer`、冷启动耗时与 Chrome 共存峰值显存（验证不溢流出 8188 MiB）。
* **M1：Chrome 采集、开源清洗与独立测试集锁定（0 CCU）**
  * 采集并标定 500–1,500 条 Chrome 外壳 + 中文网页样本（留足独立 `val` 与 `test`）；清洗约 1.5w–2.5w 条开源 Web 数据并完成质检打包。
* **M2：小样本试训与完整导出硬门槛（预估 15–25 CCU，以实测为准）**
  * 连接 A100 后前 10 分钟记录实时 `CCU/h`、预热后 `step/s`、日志中的 `IMAGE_MAX_TOKEN_NUM` 生效情况与可训练模块；
  * 用约 500 条混合数据试跑，推算正式 SFT 预计步数与时间（例：20,000 条样本、global batch 32、2 epochs ≈ `1,250` 步，加上 eval/save 估算总 CCU 并预留 25% 安全余量）；
  * **🔴 硬性通过门槛（Go/No-Go Gate）**：
    1. 成功把 LoRA 合入基础模型，且如果启用了 `freeze_vit=false` 或 `freeze_aligner=false`，**必须验证视觉侧变更已正确合入并重新导出与之匹配的 `mmproj`**（严禁新模型配原版旧 `mmproj`）；
    2. 在同一小验证集上对比 **合并后 HF 模型 / GGUF Q8_0 / GGUF Q4_K_M** 的 `Point-in-Box` 命中率、JSON 合法率与本地延迟，确认全链路无掉点才放行 M3。
* **M3：正式监督微调 SFT（预算按 M2 实测速率动态分配）**
  * **消融优先策略**：第一轮**优先冻结视觉侧（`freeze_vit=true`, `freeze_aligner=true`），只做语言侧 LoRA（`--tuner_type lora`）**，记录 `val` 指标；
  * 若存在明显视觉小图标识别短板，第二轮再开启 `--freeze_aligner false`（注意：在 `tuner_type=lora` 下，它表示**允许对 Aligner 注入 LoRA**，而非直接全参解冻；若需全参微调视觉侧同时 LLM 用 LoRA，应显式区分使用 `--tuner_type lora_llm`）做对比验证，留优去劣。
* **M4：真实任务与独立测试集验收（0 CCU 本地推理为主）**
  * 在锁定只读的 Chrome 私有 `test` 集与真机 Chrome 任务上完整验收。
* **M5：按实测决定是否上 GRPO（可选，不预先承诺提准）**
  * 若 M4 发现工程坐标完全正确，但仍存在小图标边界微偏，先补难例做二轮定向 SFT；确有余量再做小规模规则化 GRPO（`bbox` 内得 1 分，格式合法加小分）。注意：**二值 0/1 奖励在组内 4–8 次采样全对或全错时，组内相对奖励优势为零，策略损失缺少任务维度的学习信号（非所有正则损失项都零梯度），若出现大量零方差组应及时止损回到高质量难例 SFT**。

---

## 六、基于 `ms-swift` v4.x 的候选训练基线模板（待 M2 实机验证）

> 注：依据 `ms-swift` v4.3 官方 `Command-line-parameters` 与 `Qwen3_5-Best-Practice` 整理以下**候选起跑配置（尚未在本任务中实际试跑）**；包含官方 Qwen3.5 样例中的 `--add_non_thinking_prefix true` 与 `--loss_scale ignore_empty_think`。所有依赖包与参数在多模态定位样本上的实际生效情况、可训练模块清单及图像 token 数量，**须在 M2 用 500 条小样本实机验证后通过 `pip freeze` 固化**。

```bash
# Candidate dependency stack (verify in M2 and pin via `pip freeze > requirements_colab.txt`)
# ms-swift>=4.3, transformers>=5.9, qwen_vl_utils>=0.0.14, peft, liger-kernel, torchao>=0.16.0
# flash-linear-attention>=0.4.2, causal-conv1d, flash-attn==2.8.3, vllm>=0.17.0

# Baseline 1: freeze ViT and aligner first, train LLM LoRA only (recommended starting point)
PYTORCH_CUDA_ALLOC_CONF='expandable_segments:True' \
IMAGE_MAX_TOKEN_NUM=1024 \
CUDA_VISIBLE_DEVICES=0 \
swift sft \
    --model Qwen/Qwen3.5-2B \
    --use_hf true \
    --tuner_type lora \
    --target_modules all-linear \
    --lora_rank 32 \
    --lora_alpha 64 \
    --lora_dropout 0.05 \
    --freeze_vit true \
    --freeze_aligner true \
    --freeze_llm false \
    --add_non_thinking_prefix true \
    --loss_scale ignore_empty_think \
    --dataset /content/data/train_chrome_mix.jsonl \
    --val_dataset /content/data/val_chrome.jsonl \
    --torch_dtype bfloat16 \
    --per_device_train_batch_size 4 \
    --gradient_accumulation_steps 8 \
    --learning_rate 1e-4 \
    --lr_scheduler_type cosine \
    --warmup_ratio 0.05 \
    --num_train_epochs 2 \
    --max_length 4096 \
    --eval_steps 200 \
    --save_steps 200 \
    --logging_steps 10 \
    --attn_impl flash_attention_2 \
    --use_liger_kernel true \
    --dataloader_num_workers 4 \
    --output_dir /content/outputs/qwen35_2b_chrome_sft

# Ablation options (only enable if Baseline 1 shows visual alignment gaps):
# Option A (add LoRA adapters to aligner/ViT): keep --tuner_type lora, set --freeze_aligner false (optionally --freeze_vit false)
# Option B (LLM uses LoRA while aligner/ViT do full-param fine-tuning): switch to --tuner_type lora_llm with --freeze_aligner false --aligner_lr 2e-5
```

```bash
# M2 mandatory export gate: merge LoRA adapters (must also re-export matching GGUF + new mmproj if vision layers changed)
swift export \
    --adapters /content/outputs/qwen35_2b_chrome_sft/checkpoint-xxx \
    --merge_lora true \
    --torch_dtype bfloat16 \
    --output_dir /content/outputs/qwen35_2b_merged
```

---

## 七、量化验收标准（M4 最终准出指标）

正式验收严禁只报“100 次重复点击平均耗时”，必须按以下四维分项签字验收：

1. **格式与安全性**：
   * 私有 `test` 集与真机回放的 **JSON 合法率 ≥ 99%**（Schema 校验通过、坐标在 `[0, 1000]` 范围）；任何非法输出**绝不执行鼠标动作**。
2. **识别与任务准确率（分桶报告）**：
   * **Point-in-Box 命中率**（核心定位准度）：独立汇总并在 `Chrome 原生外壳`、`网页小图标（≤24px）`、`中文网页`、`常规英文网页` 四个子桶分别报告；阶段目标为综合 **≥ 85%**、Chrome 高频外壳按钮 **≥ 90%**（具体下限按 M0 原版实测基线校准，必须明显高于 M0）。
   * **动作类型准确率**（`left_click`/`scroll`/`type`/`hotkey`/`not_found` 判别）：目标 **≥ 95%**；负样本 `not_found` 召回率单独报告。
   * **多步任务完成率（1B 闭环）**：在 15–20 个覆盖打开标签、地址栏导航、中文输入搜索、关弹窗翻页的真实 Chrome 场景测试集上端到端完成率目标 **≥ 75%**（按任务语义成功条件判定）。
3. **延迟指标（保留 3 秒端到端门槛；取预热后真实负载数据，冷启动单独列明）**：
   * **单步端到端延迟 `T_step`**（截屏开始 → 预处理/模型请求/JSON 校验 → 键鼠动作发送完成，不含网页加载与动作后页面验证）：**P95 ≤ 3.0s**（目标 P50 ≤ 1.8s）。
   * **诊断指标 `T_infer`**（截屏完成 → JSON 解析完成）：同步记录 P50 / P95，用于拆解推理端与本地前处理/控制端各自耗时。
   * **冷启动耗时**（首次加载模型与首帧推理）单独统计列明；若实测在 RTX 4060 Laptop 上无法达到 `T_step P95 ≤ 3.0s`，须显式向用户汇报并协商目标或分辨率折中方案。
4. **本地资源稳定性（RTX 4060 Laptop 8188 MiB + 多标签 Chrome 共存）**：
   * 连续执行 30 分钟混合网页任务期间，`nvidia-smi` 记录模型服务 + Chrome 合计**专用显存峰值 < 7.5 GiB**，**零“系统共享 GPU 内存”溢流**，无 OOM 或显存持续泄漏。
