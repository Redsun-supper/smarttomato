# old/ —— 历史版本归档

这里放的是**已被取代**的历史代码，**不是当前上板程序**，仅作参考与追溯。

> 📌 **当前上板程序是仓库根目录的 `m10_step3_away_last.py`。**

## 文件清单

| 文件 | 阶段 | 说明 |
|---|---|---|
| `m10_step1_clock.py` | 第 1 段 | 横屏时钟 + 日期。含 `ClockRenderer`、中英混排分段取字体、`fit_size` 字号自适应、`--preview`。已被第 2 段取代。 |
| `m10_step2_focus_last.py` | 第 2 段 | 语音「开始学习」→ TTS 应答 → 番茄钟；时间/日期丝滑缩小飞向左上角。含 `FocusApp`、`Screen`、**文字精灵缓存**、帧率统计、`/tmp/step2_trigger` 钩子、A/B 键、`--demo` / `--preview`。**第 2 段的最终版**（`_last` 后缀），已被第 3 段取代。 |
| `m10_focus_clock.py` | 早期 | 番茄钟早期实现。 |
| `m10_voice_clock.py` | 早期 | 语音唤醒时钟。含 `VoiceASR` / `TTSSpeaker` / `VoiceMonitor` 三个封装类 —— **这部分仍有参考价值**，后续脚本的语音部分是从这里演化来的。 |
| `m10_landscape_clock.py` | 早期 | 最早的横屏时钟。 |
| `test_wake_tts.py` | 早期 | 唤醒 / TTS 的验证脚本。 |

## 注意

- 这些脚本里的**参数名与行为可能已过时**（例如 `m10_voice_clock.py` 的唤醒词是「老头子」、
  `REPLY_MODE` 等），不要把它们的配置当作当前入口。当前参数见根目录 `README.md` §7。
- 归档时用的是 `git mv`，**历史提交记录完整保留**，可以直接 `git log --follow <文件>` 追溯。
- 配套文档：`docs/m10_step2_focus_last.md`（第 2 段）、`docs/m10_step3_away_last.md`（第 3 段）。
