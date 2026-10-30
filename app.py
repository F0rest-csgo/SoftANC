"""SoftANC: experimental microphone polarity inversion, not calibrated ANC."""
import math
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
import sounddevice as sd
from dsp import Processor


class App:
    def __init__(self, root, config_path=None):
        self.root = root
        self.config_path = Path(config_path) if config_path else Path(__file__).with_name('settings.json')
        self.save_job = None
        self.saved_devices = {}
        root.title('SoftANC · 低延迟反相实验台')
        root.geometry('860x810')
        root.minsize(780, 790)
        self.stream = None
        self.processor = None
        self.fault = None
        self.xruns = 0
        self.callback_ms = 0
        self.target = -.05
        self.threshold_db = -30.0
        self.threshold = tk.DoubleVar(value=-30)
        self.threshold_enabled = tk.BooleanVar(value=True)
        self.gate_status = tk.StringVar(value='音量门控：待机')
        self.gain = tk.DoubleVar(value=5)
        self.invert = tk.BooleanVar(value=True)
        self.exclusive = tk.BooleanVar(value=False)
        self.confirm = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='待机 · 尚未采集音频')
        self.metrics = tk.StringVar(value='启动后显示音频统计')
        self.level = tk.StringVar(value='输入 —    输出 —')
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TLabel', font=('Microsoft YaHei UI', 10))
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 23, 'bold'))
        style.configure('TButton', padding=8)
        main = ttk.Frame(root, padding=24)
        main.pack(fill='both', expand=True)
        ttk.Label(main, text='SoftANC', style='Title.TLabel').pack(anchor='w')
        ttk.Label(main, text='麦克风 → 极性反转 → 耳机 / 实时音频实验', foreground='#436480').pack(anchor='w', pady=(0, 16))
        ttk.Label(main, text='反相不等于有效降噪：声学路径和处理延迟可能使噪声更响。\n请使用有线耳机，从低系统音量开始；出现啸叫立即按 Esc 停止。', foreground='#a34b16').pack(anchor='w', pady=(0, 12))
        form = ttk.Frame(main)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        self.inputs = self.combo(form, 0, '麦克风（使用第 1 声道）')
        self.outputs = self.combo(form, 1, '耳机输出（单声道复制至左右）')
        self.rate = self.combo(form, 2, '采样率 / Hz', ['48000', '44100', '96000'])
        self.block = self.combo(form, 3, '缓冲帧数（0 = 驱动自动）', ['0', '64', '128', '256', '512'])
        self.delay = self.combo(form, 4, '额外延时 / ms（不能抵消已有延迟）', ['0', '0.1', '0.25', '0.5', '1', '2', '5', '10', '20'])
        self.configs = [self.inputs, self.outputs, self.rate, self.block, self.delay]
        self.exclusive_box = ttk.Checkbutton(main, text='WASAPI 独占模式（设备需支持；可能占用其他应用音频）', variable=self.exclusive)
        self.exclusive_box.pack(anchor='w', pady=10)
        control = ttk.LabelFrame(main, text='实时控制', padding=12)
        control.pack(fill='x')
        self.gain_label = ttk.Label(control, text='输出增益 5%')
        self.gain_label.pack(anchor='w')
        ttk.Scale(control, from_=0, to=100, variable=self.gain, command=self.update_target).pack(fill='x')
        ttk.Checkbutton(control, text='反转极性（取消勾选可对照；0% 为静音）', variable=self.invert, command=self.update_target).pack(anchor='w')
        ttk.Checkbutton(control, text='启用音量阈值：达到阈值时静音，不转为原声播放', variable=self.threshold_enabled, command=self.update_threshold).pack(anchor='w')
        self.threshold_label = ttk.Label(control, text='静音阈值 -30 dBFS（输入峰值；越低越容易触发）')
        self.threshold_label.pack(anchor='w')
        ttk.Scale(control, from_=-80, to=0, variable=self.threshold, command=self.update_threshold).pack(fill='x')
        ttk.Label(control, textvariable=self.gate_status).pack(anchor='w')
        ttk.Checkbutton(main, text='我已选择耳机输出，并调低系统音量', variable=self.confirm).pack(anchor='w', pady=12)
        buttons = ttk.Frame(main)
        buttons.pack(fill='x')
        self.start_button = ttk.Button(buttons, text='开始处理', command=self.start)
        self.start_button.pack(side='left')
        ttk.Button(buttons, text='立即停止 / Esc', command=self.stop).pack(side='left', padx=8)
        self.refresh_button = ttk.Button(buttons, text='刷新设备', command=self.refresh)
        self.refresh_button.pack(side='right')
        ttk.Label(main, textvariable=self.status, wraplength=790).pack(anchor='w', pady=(14, 4))
        ttk.Label(main, textvariable=self.level).pack(anchor='w')
        ttk.Label(main, textvariable=self.metrics, wraplength=790).pack(anchor='w', pady=5)
        root.bind('<Escape>', lambda e: self.stop())
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.refresh()
        self.load_settings()
        for widget in self.configs:
            widget.bind('<<ComboboxSelected>>', self.settings_changed)
        for variable in [self.gain, self.invert, self.exclusive, self.threshold, self.threshold_enabled]:
            variable.trace_add('write', self.settings_changed)
        self.root.after(100, self.poll)

    def device_key(self, widget, ids):
        index = widget.current()
        if index < 0 or index >= len(ids):
            return None
        device = self.devices[ids[index]]
        return {'name': device['name'], 'api': self.apis[device['hostapi']]['name']}

    def load_settings(self):
        try:
            if not self.config_path.exists():
                return
            data = json.loads(self.config_path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('配置必须是 JSON 对象')
            for key, widget in [('rate', self.rate), ('block', self.block), ('delay', self.delay)]:
                if data.get(key) in widget['values']:
                    widget.set(data[key])
            for key, var, low, high in [('gain', self.gain, 0, 100), ('threshold', self.threshold, -80, 0)]:
                value = data.get(key)
                if type(value) in (int, float) and math.isfinite(value) and low <= value <= high:
                    var.set(value)
            for key, var in [('invert', self.invert), ('exclusive', self.exclusive), ('threshold_enabled', self.threshold_enabled)]:
                if type(data.get(key)) is bool:
                    var.set(data[key])
            missing = False
            for key, widget, ids in [('input', self.inputs, self.in_ids), ('output', self.outputs, self.out_ids)]:
                wanted = data.get(key)
                if not isinstance(wanted, dict):
                    continue
                self.saved_devices[key] = wanted
                match = next((n for n, i in enumerate(ids) if wanted == {'name': self.devices[i]['name'], 'api': self.apis[self.devices[i]['hostapi']]['name']}), None)
                if match is None:
                    widget.set('')
                    missing = True
                else:
                    widget.current(match)
            self.update_target()
            self.update_threshold()
            self.status.set('配置已恢复 · 保存的设备未连接，请重新选择' if missing else '配置已恢复 · 待机')
        except (OSError, ValueError) as exc:
            self.status.set(f'配置读取失败，使用默认设置：{exc}')

    def settings_changed(self, *_):
        self.update_target()
        self.update_threshold()
        if self.save_job is not None:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(350, self.save_settings)

    def save_settings(self):
        if self.save_job is not None:
            self.root.after_cancel(self.save_job)
            self.save_job = None
        data = {key: var.get() for key, var in [('gain', self.gain), ('invert', self.invert), ('exclusive', self.exclusive), ('threshold', self.threshold), ('threshold_enabled', self.threshold_enabled)]}
        data.update({key: widget.get() for key, widget in [('rate', self.rate), ('block', self.block), ('delay', self.delay)]})
        for key, widget, ids in [('input', self.inputs, self.in_ids), ('output', self.outputs, self.out_ids)]:
            data[key] = self.device_key(widget, ids) or self.saved_devices.get(key)
        try:
            temporary = self.config_path.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
            temporary.replace(self.config_path)
        except (OSError, ValueError) as exc:
            self.status.set(f'配置保存失败：{exc}')

    def combo(self, parent, row, label, values=()):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', padx=(0, 15), pady=5)
        widget = ttk.Combobox(parent, values=values, state='readonly', width=48)
        widget.grid(row=row, column=1, sticky='ew', pady=5)
        if values:
            widget.current(0)
        return widget

    def refresh(self):
        try:
            if hasattr(self, 'in_ids'):
                self.save_settings()
            self.devices = sd.query_devices()
            self.apis = sd.query_hostapis()
            self.in_ids = [i for i, d in enumerate(self.devices) if d['max_input_channels']]
            self.out_ids = [i for i, d in enumerate(self.devices) if d['max_output_channels']]
            for direction, (widget, ids) in enumerate([(self.inputs, self.in_ids), (self.outputs, self.out_ids)]):
                widget['values'] = [f"{i}: {self.devices[i]['name']} [{self.apis[self.devices[i]['hostapi']]['name']}]" for i in ids]
                if ids:
                    default_id = sd.default.device[direction]
                    default_name = self.devices[default_id]['name'] if default_id >= 0 else ''
                    preferred = next((n for n, i in enumerate(ids) if self.devices[i]['name'] == default_name and 'WASAPI' in self.apis[self.devices[i]['hostapi']]['name']), next((n for n, i in enumerate(ids) if 'WASAPI' in self.apis[self.devices[i]['hostapi']]['name']), 0))
                    widget.current(preferred)
            self.status.set('待机 · 请选择麦克风和耳机；输入输出须使用相同音频后端')
            if hasattr(self, 'in_ids') and self.config_path.exists():
                self.load_settings()
        except Exception as exc:
            self.status.set(f'设备枚举失败：{exc}')

    def update_target(self, *_):
        value = self.gain.get()
        self.target = value / 100 * (-1 if self.invert.get() else 1)
        self.gain_label.configure(text=f'输出增益 {value:.0f}% · 数字峰值上限 -12 dBFS')

    def callback(self, indata, outdata, frames, times, flags):
        try:
            if flags:
                self.xruns += 1
            self.processor.process(indata, outdata, self.target, self.threshold_db)
            self.callback_ms = max(0, (times.outputBufferDacTime - times.inputBufferAdcTime) * 1000)
        except Exception as exc:
            outdata.fill(0)
            self.fault = exc
            raise sd.CallbackAbort

    def update_threshold(self, *_):
        value = self.threshold.get()
        self.threshold_db = value if self.threshold_enabled.get() else None
        self.threshold_label.configure(text=f'静音阈值 {value:.1f} dBFS（输入峰值；越低越容易触发）')

    def lock_config(self, running):
        for widget in self.configs:
            widget.configure(state='disabled' if running else 'readonly')
        for widget in [self.start_button, self.refresh_button, self.exclusive_box]:
            widget.configure(state='disabled' if running else 'normal')

    def start(self):
        if self.stream is not None:
            return
        if not self.confirm.get():
            messagebox.showinfo('耳机测试', '请先选择耳机、调低系统音量，并勾选确认。')
            return
        try:
            if self.inputs.current() < 0 or self.outputs.current() < 0:
                raise ValueError('未找到可用输入或输出设备')
            inp, out = self.in_ids[self.inputs.current()], self.out_ids[self.outputs.current()]
            a, b = self.devices[inp], self.devices[out]
            if a['hostapi'] != b['hostapi']:
                raise ValueError('输入和输出必须选用相同音频后端，例如均为 Windows WASAPI')
            is_wasapi = 'WASAPI' in self.apis[a['hostapi']]['name']
            if self.exclusive.get() and not is_wasapi:
                raise ValueError('独占选项仅适用于 WASAPI 设备')
            extra = sd.WasapiSettings(exclusive=self.exclusive.get()) if is_wasapi else None
            rate = int(self.rate.get())
            # Shared WASAPI often requires the endpoint channel count.
            in_channels = min(2, a['max_input_channels'])
            out_channels = min(2, b['max_output_channels'])
            sd.check_input_settings(device=inp, channels=in_channels, dtype='float32', samplerate=rate, extra_settings=extra)
            sd.check_output_settings(device=out, channels=out_channels, dtype='float32', samplerate=rate, extra_settings=extra)
            self.processor = Processor(rate, float(self.delay.get()))
            self.fault, self.xruns, self.callback_ms = None, 0, 0
            self.stream = sd.Stream(device=(inp, out), samplerate=rate, blocksize=int(self.block.get()), channels=(in_channels, out_channels), dtype='float32', latency='low', extra_settings=(extra, extra), callback=self.callback)
            self.stream.start()
            self.lock_config(True)
            self.status.set('运行中 · Esc 立即停止')
        except Exception as exc:
            self.stop()
            self.status.set(f'启动失败：{exc}')
            messagebox.showerror('无法打开音频设备', f'{exc}\n\n请尝试 48000/44100 Hz、关闭独占模式，或选择同一声卡的输入输出。')

    def stop(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.abort()
            finally:
                stream.close()
        self.lock_config(False)
        self.status.set('已停止 · 输出关闭')
        self.gate_status.set('音量门控：待机')

    def poll(self):
        if self.stream is not None:
            if self.fault is not None or not self.stream.active:
                fault = self.fault
                self.stop()
                self.status.set(f'音频已中断：{fault or "设备断开或流停止"}')
            else:
                p = self.processor
                self.gate_status.set('音量门控：已关闭' if self.threshold_db is None else ('音量门控：已静音（达到阈值 / 200 ms 保持）' if p.gated else '音量门控：低于阈值，正常处理'))
                db = lambda v: f'{20 * math.log10(v):.1f}' if v > 0 else '-∞'
                self.level.set(f'输入峰值 {db(p.input_peak)} dBFS    输出峰值 {db(p.output_peak)} dBFS    {"限幅中" if p.limited else ""}')
                lat = self.stream.latency
                self.metrics.set(f'驱动报告输入 / 输出：{lat[0]*1000:.1f} / {lat[1]*1000:.1f} ms    回调时间戳跨度：{self.callback_ms:.1f} ms\n额外延时：{p.delay / self.stream.samplerate * 1000:.2f} ms    回调负载：{self.stream.cpu_load*100:.1f}%    异常缓冲次数：{self.xruns}\n以上不是实测声学延迟或降噪量；数字限幅不能保证耳内音量安全。')
        self.root.after(100, self.poll)

    def close(self):
        self.stop()
        self.save_settings()
        self.root.destroy()


if __name__ == '__main__':
    App(tk.Tk()).root.mainloop()
