import tkinter as tk
from tkinter import ttk, messagebox
import subprocess
import threading
import os
import time
import urllib.request
import json

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

def run_ollama(args, timeout=30):
    try:
        result = subprocess.run(
            ["ollama"] + args, capture_output=True, timeout=timeout,
            env={**os.environ, "OLLAMA_HOST": OLLAMA_URL}
        )
        out = result.stdout.decode("utf-8", errors="replace").strip()
        return out, result.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "", -1

def get_gpu_info():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.free,memory.used",
                           "--format=csv,noheader"], capture_output=True, timeout=5)
        out = r.stdout.decode("utf-8", errors="replace").strip().replace(" MiB", "")
        parts = out.split(", ")
        if len(parts) == 3:
            return int(parts[0]), int(parts[1]), int(parts[2])
    except:
        pass
    return None, None, None

def parse_size_parts(parts, start_idx):
    if start_idx >= len(parts):
        return 0, start_idx + 1
    val = parts[start_idx]
    if start_idx + 1 < len(parts) and parts[start_idx + 1].upper() in ("GB", "MB"):
        val = val + " " + parts[start_idx + 1]
        consumed = 2
    else:
        consumed = 1
    s = val.upper().replace(" ", "")
    try:
        if "GB" in s:
            return float(s.replace("GB", "")) * 1024, start_idx + consumed
        elif "MB" in s:
            return float(s.replace("MB", "")), start_idx + consumed
    except:
        pass
    return 0, start_idx + consumed

def fmt_size(mb):
    if mb >= 1024:
        return f"{mb/1024:.1f} GB"
    if mb >= 1:
        return f"{mb:.0f} MB"
    return "0 MB"

def can_load(model_size_mb, vram_free_mb, ram_free_mb):
    if vram_free_mb and model_size_mb <= vram_free_mb * 0.85:
        return "ok", "显存充足"
    elif vram_free_mb and model_size_mb <= vram_free_mb:
        return "warn", "显存勉强"
    elif ram_free_mb and model_size_mb <= ram_free_mb * 0.7:
        return "warn", "需用内存"
    elif ram_free_mb and model_size_mb <= ram_free_mb:
        return "warn", "内存勉强"
    return "fail", "资源不足"

def verify_model_loaded(name, max_wait=60):
    """轮询 ollama ps 确认模型是否成功加载。返回 (success, detail)"""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        ps_out, _ = run_ollama(["ps"], timeout=10)
        if ps_out and name in ps_out:
            # 提取上下文大小和处理器信息
            for line in ps_out.strip().split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[0] == name:
                    size_mb, idx = parse_size_parts(parts, 1)
                    proc = " ".join(parts[idx+1:idx+3]) if idx+2 < len(parts) else parts[-1]
                    ctx = parts[-2] if len(parts) >= 6 else "?"
                    return True, f"{name} 已缓存 · {fmt_size(size_mb)} · {proc} · context={ctx}"
        time.sleep(2)
    return False, f"{name} 缓存失败（{max_wait}秒内未出现在 ollama ps 中）"

class OllamaManager:
    def __init__(self, root):
        self.root = root
        self.root.title("Ollama 模型管理器")
        self.root.geometry("900x680")
        self.root.minsize(800, 550)
        self.root.configure(bg="#1e1e2e")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background="#2a2a3e", foreground="#cdd6f4",
                        fieldbackground="#2a2a3e", rowheight=28)
        style.configure("Treeview.Heading", background="#313244", foreground="#cdd6f4",
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#45475a")])
        style.configure("TButton", font=("Microsoft YaHei UI", 10))
        style.configure("TLabel", background="#1e1e2e", foreground="#cdd6f4",
                        font=("Microsoft YaHei UI", 10))
        style.configure("Recommend.TButton", font=("Microsoft YaHei UI", 10, "bold"))

        # ===== 资源监控区 =====
        res_frame = tk.LabelFrame(root, text="系统资源", bg="#1e1e2e", fg="#cba6f7",
                                  font=("Microsoft YaHei UI", 10, "bold"), padx=10, pady=5)
        res_frame.pack(fill=tk.X, padx=15, pady=(10, 5))
        for label, varname in [("内存", "ram"), ("显存", "vram")]:
            f = tk.Frame(res_frame, bg="#1e1e2e"); f.pack(fill=tk.X, pady=2)
            tk.Label(f, text=label, bg="#1e1e2e", fg="#cdd6f4",
                     font=("Microsoft YaHei UI", 9), width=5, anchor="e").pack(side=tk.LEFT, padx=(0, 5))
            bar = ttk.Progressbar(f, length=300, mode="determinate")
            bar.pack(side=tk.LEFT)
            txt = tk.Label(f, text="", bg="#1e1e2e", fg="#a6adc8",
                           font=("Microsoft YaHei UI", 9), width=44, anchor="w")
            txt.pack(side=tk.LEFT, padx=5)
            setattr(self, f"{varname}_bar", bar)
            setattr(self, f"{varname}_text", txt)

        # ===== 已加载模型 =====
        lf = tk.LabelFrame(root, text="当前已加载", bg="#1e1e2e", fg="#f38ba8",
                           font=("Microsoft YaHei UI", 10, "bold"), padx=10, pady=5)
        lf.pack(fill=tk.X, padx=15, pady=5)
        self.loaded_tree = ttk.Treeview(lf, columns=("name", "size", "proc"), show="headings", height=2)
        self.loaded_tree.heading("name", text="模型名"); self.loaded_tree.column("name", width=480)
        self.loaded_tree.heading("size", text="大小"); self.loaded_tree.column("size", width=100)
        self.loaded_tree.heading("proc", text="处理器"); self.loaded_tree.column("proc", width=110)
        self.loaded_tree.pack(fill=tk.X, pady=2)
        bf = tk.Frame(lf, bg="#1e1e2e"); bf.pack(fill=tk.X)
        ttk.Button(bf, text="释放选中", command=self.unload_selected).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(bf, text="释放全部", command=self.unload_all).pack(side=tk.LEFT)
        ttk.Button(bf, text="刷新", command=self.refresh).pack(side=tk.RIGHT)

        # ===== 已安装模型 + 预览 =====
        self.main_frame = tk.LabelFrame(root, text="已安装模型", bg="#1e1e2e", fg="#a6e3a1",
                                        font=("Microsoft YaHei UI", 10, "bold"), padx=10, pady=5)
        self.main_frame.pack(fill=tk.BOTH, padx=15, pady=(5, 0), expand=True)

        # 新增缓存状态列
        self.all_tree = ttk.Treeview(self.main_frame, columns=("name", "size", "cache", "status"), show="headings")
        self.all_tree.heading("name", text="模型名"); self.all_tree.column("name", width=320)
        self.all_tree.heading("size", text="大小"); self.all_tree.column("size", width=90)
        self.all_tree.heading("cache", text="缓存"); self.all_tree.column("cache", width=80)
        self.all_tree.heading("status", text="加载评估"); self.all_tree.column("status", width=200)
        self.all_tree.pack(fill=tk.BOTH, expand=True, pady=2)
        self.all_tree.tag_configure("ok", foreground="#a6e3a1")
        self.all_tree.tag_configure("warn", foreground="#f9e2af")
        self.all_tree.tag_configure("fail", foreground="#f38ba8")
        self.all_tree.tag_configure("loaded", background="#45475a", foreground="#cba6f7")
        self.all_tree.tag_configure("recommend", background="#313244", foreground="#a6e3a1",
                                    font=("Microsoft YaHei UI", 10, "bold"))
        self.all_tree.bind("<<TreeviewSelect>>", self.on_model_select)

        self.preview_container = tk.Frame(self.main_frame, bg="#1e1e2e")

        self.preview_label = tk.Label(self.preview_container, text="选择一个模型查看占用预估",
                                      bg="#1e1e2e", fg="#6c7086",
                                      font=("Microsoft YaHei UI", 9), anchor="w")
        self.preview_label.pack(fill=tk.X, pady=(2, 0))

        self.preview_vram_bar = ttk.Progressbar(self.preview_container, length=200, mode="determinate")
        self.preview_vram_bar.pack(fill=tk.X, pady=1)

        self.preview_ram_bar = ttk.Progressbar(self.preview_container, length=200, mode="determinate")
        self.preview_ram_bar.pack(fill=tk.X, pady=(0, 2))

        bf2 = tk.Frame(self.main_frame, bg="#1e1e2e"); bf2.pack(fill=tk.X, pady=(5, 2))
        ttk.Button(bf2, text="加载选中", command=self.load_selected).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(bf2, text="释放选中", command=self.unload_selected_name).pack(side=tk.LEFT, padx=(0, 10))
        self.recommend_btn = ttk.Button(bf2, text="一键加载推荐模型", style="Recommend.TButton",
                                        command=self.load_recommended)
        self.recommend_btn.pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(bf2, text="刷新列表", command=self.refresh).pack(side=tk.RIGHT)

        self.status = ttk.Label(root, text="就绪", relief=tk.SUNKEN, anchor=tk.W)
        self.status.pack(fill=tk.X, side=tk.BOTTOM)

        self.models_data = {}
        self.loaded_names = set()
        self.recommended_model = None
        self._refreshing = False
        self._selected_name = None
        self._current_ram_free_mb = 0
        self.refresh()
        self.auto_refresh()

    def on_model_select(self, event):
        if self._refreshing:
            return
        sel = self.all_tree.selection()
        if not sel:
            self._hide_preview()
            return
        name = self.all_tree.item(sel[0])["values"][0]
        self._selected_name = name
        size_mb = self.models_data.get(name, 0)
        self._update_preview(name, size_mb)

    def _hide_preview(self):
        self.preview_container.pack_forget()

    def _show_preview(self):
        try:
            self.preview_container.pack_info()
        except tk.TclError:
            self.preview_container.pack(fill=tk.X, pady=(5, 0))

    def _update_preview(self, name, size_mb):
        import psutil
        self._show_preview()

        vram_total, vram_free, vram_used = get_gpu_info()
        vram_total = vram_total or 0
        vram_free = vram_free or 0
        vram_used = vram_used or 0
        ram_free = self._current_ram_free_mb

        if vram_total:
            new_vram = min(vram_used + size_mb, vram_total)
            self.preview_vram_bar["value"] = new_vram / vram_total * 100
            after_vram = max(0, vram_free - size_mb)
            vram_info = f"加载后显存剩余 {fmt_size(after_vram)}"
        else:
            self.preview_vram_bar["value"] = 0
            vram_info = ""

        mem = psutil.virtual_memory()
        ram_total = mem.total / (1024*1024)
        new_ram_pct = (mem.used / (1024*1024) + size_mb) / ram_total * 100
        self.preview_ram_bar["value"] = min(new_ram_pct, 100)
        after_ram = max(0, ram_free - size_mb)

        text = f"{name} · {fmt_size(size_mb)}  |  加载后内存剩余 {fmt_size(after_ram)}"
        if vram_info:
            text += f"  |  {vram_info}"

        code, _, _ = can_load(size_mb, vram_free, ram_free)
        color = {"ok": "#a6e3a1", "warn": "#f9e2af", "fail": "#f38ba8"}.get(code, "#cdd6f4")
        self.preview_label.config(text=text, fg=color)

    def auto_refresh(self):
        self.refresh(quiet=True)
        self.root.after(3000, self.auto_refresh)

    def refresh(self, quiet=False):
        threading.Thread(target=self._do_refresh, args=(quiet,), daemon=True).start()

    def _do_refresh(self, quiet=False):
        import psutil
        self._refreshing = True

        mem = psutil.virtual_memory()
        ram_total = mem.total / (1024*1024)
        ram_used = mem.used / (1024*1024)
        ram_free = mem.available / (1024*1024)
        ram_pct = mem.percent
        self._current_ram_free_mb = ram_free

        vram_total, vram_free, vram_used = get_gpu_info()
        vram_total = vram_total or 0
        vram_free = vram_free or 0
        vram_used = vram_used or 0
        vram_pct = (vram_used / vram_total * 100) if vram_total else 0

        def fmt_dual(mb):
            if mb >= 1024:
                return f"{mb:.0f} MB ({mb/1024:.1f} GB)"
            return f"{mb:.0f} MB"

        def update_bars():
            self.ram_bar["value"] = ram_pct
            self.ram_text.config(text=f"已用 {fmt_dual(ram_used)} / 总量 {fmt_dual(ram_total)}  |  可用 {fmt_dual(ram_free)}")
            if ram_pct > 90: self.ram_text.config(fg="#f38ba8")
            elif ram_pct > 70: self.ram_text.config(fg="#f9e2af")
            else: self.ram_text.config(fg="#a6adc8")
            if vram_total:
                self.vram_bar["value"] = vram_pct
                self.vram_text.config(text=f"已用 {fmt_dual(vram_used)} / 总量 {fmt_dual(vram_total)}  |  可用 {fmt_dual(vram_free)}")
                if vram_pct > 90: self.vram_text.config(fg="#f38ba8")
                elif vram_pct > 70: self.vram_text.config(fg="#f9e2af")
                else: self.vram_text.config(fg="#a6adc8")
            else:
                self.vram_bar["value"] = 0
                self.vram_text.config(text="未检测到 NVIDIA GPU")
        self.root.after(0, update_bars)

        saved_selection = self._selected_name

        # 已加载模型
        ps_out, _ = run_ollama(["ps"])
        self.loaded_names = set()
        self.root.after(0, lambda: self.loaded_tree.delete(*self.loaded_tree.get_children()))
        if ps_out:
            for line in ps_out.strip().split("\n")[1:]:
                parts = line.split()
                if len(parts) < 3:
                    continue
                name = parts[0]
                size_mb, idx = parse_size_parts(parts, 1)
                size_label = fmt_size(size_mb)
                proc = " ".join(parts[idx+1:idx+3]) if idx+2 < len(parts) else parts[-1]
                self.loaded_names.add(name)
                self.root.after(0, lambda n=name, s=size_label, p=proc:
                                self.loaded_tree.insert("", "end", values=(n, s, p)))

        # 所有模型
        list_out, _ = run_ollama(["list"])
        self.models_data = {}
        self.recommended_model = None
        best_fit = 0
        self.root.after(0, lambda: self.all_tree.delete(*self.all_tree.get_children()))
        if list_out:
            for line in list_out.strip().split("\n")[1:]:
                parts = line.split()
                if len(parts) < 3:
                    continue
                name = parts[0]
                size_mb, _ = parse_size_parts(parts, 2)
                self.models_data[name] = size_mb
                code, msg = can_load(size_mb, vram_free, ram_free)
                size_label = fmt_size(size_mb)
                cache_label = "已缓存" if name in self.loaded_names else "未缓存"

                if name in self.loaded_names:
                    tag = "loaded"
                elif code == "ok" and size_mb > best_fit:
                    best_fit = size_mb
                    self.recommended_model = name
                    tag = "recommend"
                elif code == "ok":
                    tag = "ok"
                else:
                    tag = code
                self.root.after(0, lambda n=name, s=size_label, c=cache_label, m=msg, t=tag:
                                self.all_tree.insert("", "end", values=(n, s, c, m), tags=(t,)))

        if saved_selection and saved_selection in self.models_data:
            def find_and_select():
                for item in self.all_tree.get_children():
                    if self.all_tree.item(item)["values"][0] == saved_selection:
                        self.all_tree.selection_set(item)
                        self.all_tree.see(item)
                        break
            self.root.after(100, find_and_select)

        loaded_n = len(self.loaded_names)
        all_n = len(self.models_data)
        rec_text = f" | 推荐: {self.recommended_model}" if self.recommended_model else ""
        self.root.after(0, lambda: self.status.config(
            text=f"就绪 | {all_n} 个模型 | {loaded_n} 个已加载{rec_text} | 内存 {ram_pct:.0f}% | 显存 {vram_pct:.0f}%"))

        if self.recommended_model and self.recommended_model not in self.loaded_names:
            self.root.after(0, lambda: self.recommend_btn.config(
                text=f"一键加载推荐: {self.recommended_model}", state="normal"))
        else:
            self.root.after(0, lambda: self.recommend_btn.config(text="无需加载", state="disabled"))

        self._refreshing = False

    def unload_selected(self):
        sel = self.loaded_tree.selection()
        if not sel: messagebox.showinfo("提示", "请先选中一个已加载模型"); return
        self._unload(self.loaded_tree.item(sel[0])["values"][0])

    def unload_selected_name(self):
        sel = self.all_tree.selection()
        if not sel: messagebox.showinfo("提示", "请先选中一个模型"); return
        self._unload(self.all_tree.item(sel[0])["values"][0])

    def _unload(self, name):
        self.status.config(text=f"正在释放 {name}...")
        def task():
            run_ollama(["stop", name])
            self.root.after(0, lambda: self.refresh())
        threading.Thread(target=task, daemon=True).start()

    def unload_all(self):
        items = self.loaded_tree.get_children()
        if not items: return
        if not messagebox.askyesno("确认", f"确定释放全部 {len(items)} 个模型？"): return
        self.status.config(text="正在释放全部...")
        def task():
            for i in items:
                run_ollama(["stop", self.loaded_tree.item(i)["values"][0]])
            self.root.after(0, lambda: self.refresh())
        threading.Thread(target=task, daemon=True).start()

    def load_recommended(self):
        if not self.recommended_model:
            messagebox.showinfo("提示", "所有合适的模型已加载"); return
        self._load(self.recommended_model, self.models_data.get(self.recommended_model, 0))

    def load_selected(self):
        sel = self.all_tree.selection()
        if not sel: messagebox.showinfo("提示", "请先选中一个模型"); return
        name = self.all_tree.item(sel[0])["values"][0]
        self._load(name, self.models_data.get(name, 0))

    def _load(self, name, size_mb):
        import psutil
        mem = psutil.virtual_memory()
        ram_free = mem.available / (1024*1024)
        _, vram_free, _ = get_gpu_info()
        vram_free = vram_free or 0
        code, msg = can_load(size_mb, vram_free, ram_free)
        if code == "fail":
            if not messagebox.askyesno("资源不足",
                                       f"加载 {name}（{fmt_size(size_mb)}）\n"
                                       f"显存可用 {fmt_size(vram_free)} | 内存可用 {fmt_size(ram_free)}\n\n"
                                       f"很可能失败，仍要尝试？"):
                return
        elif code == "warn":
            if not messagebox.askyesno("资源紧张",
                                       f"加载 {name}（{fmt_size(size_mb)}）\n"
                                       f"显存可用 {fmt_size(vram_free)} | 内存可用 {fmt_size(ram_free)}\n\n"
                                       f"性能可能下降，仍要加载？"):
                return
        self.status.config(text=f"正在加载 {name}（此过程可能需要几十秒）...")
        def task():
            # 发送 API 请求预热模型
            payload = json.dumps({"model": name, "keep_alive": -1}).encode("utf-8")
            try:
                req = urllib.request.Request(
                    f"http://localhost:11434/api/generate",
                    data=payload,
                    headers={"Content-Type": "application/json"}
                )
                urllib.request.urlopen(req, timeout=120)
            except Exception:
                pass
            time.sleep(3)
            # 验证是否成功加载
            ok, detail = verify_model_load(name)
            def show_result():
                if ok:
                    self.status.config(text=detail)
                    messagebox.showinfo("加载成功", detail)
                else:
                    self.status.config(text=detail)
                    messagebox.showerror("加载失败", detail)
                self.refresh()
            self.root.after(0, show_result)
        threading.Thread(target=task, daemon=True).start()

if __name__ == "__main__":
    root = tk.Tk()
    app = OllamaManager(root)
    root.mainloop()
