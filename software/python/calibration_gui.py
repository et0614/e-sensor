"""
E-Sensor 複数風洞 校正GUI (Tkinter, Python 3.12)

運用モデル(ユーザ設計):
  - 風洞ごとに1行。初期状態はどのポートも未割当。
  - 新しいUSBポートで E-Sensor が検出されたら、未割当の風洞のどれに割り当てるか
    ダイアログで選ぶ。選んだら「そのポート ↔ その風洞」は以後固定。
  - 割当済み風洞に個体が在れば[開始]が有効化。押すと verify_device → 校正 を実行。
  - 個体が抜かれたら「空き」に戻る(割当=固定は保持。校正中なら中断)。
  - 各風洞は独立ワーカーで随時開始でき、他風洞の校正中でも別風洞を開始できる。

ポート↔個体↔MIDIの対応は esensor_discovery が担う(device_id == USBシリアル、
MIDIポート名が重複しても CMD_REQ_ID で個体確定)。

実行: py -3.12 calibration_gui.py
"""
import os
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

# 校正ワーカーは別スレッドで matplotlib を「保存」に使う。GUIバックエンドだと
# 別スレッド描画で問題が出るため、非対話の Agg を強制する(校正は show_plot=False)。
import matplotlib
matplotlib.use('Agg')

import esensor_discovery as disc
import verify_device
from calibrate_coefficients import (
    AnemometerCalibrator, CALIBRATOR_PROFILES, OUTPUT_DIR,
)

POLL_INTERVAL_MS = 1500

# 抜線はこの回数だけ連続で不在を確認してから確定する(デバウンス)。ハブは1台
# 抜くと一瞬、他ポートも再列挙で消えることがあり、1周の取りこぼしで「抜かれた」と
# 誤判定(＝校正中の中断)しないため。2 なら最大 ~2 周(約3秒)で確定。
REMOVE_DEBOUNCE_MISSES = 2

# 風洞は CALIBRATOR_PROFILES に定義された ID ぶんだけ用意する。
# 風洞 id N は fan_index=N・プロファイル=CALIBRATOR_PROFILES[N]。
# (4風洞運用にはプロファイル 3,4 を calibrate_coefficients.py に追加すること)
TUNNEL_IDS = sorted(CALIBRATOR_PROFILES.keys())

# 状態表示の色
STATUS_STYLE = {
    'free':        ('空き',       '#888888'),
    'ready':       ('占有(準備完了)', '#1a7f37'),
    'verifying':   ('動作確認中',   '#0969da'),
    'calibrating': ('校正中',      '#0969da'),
    'done':        ('完了',        '#1a7f37'),
    'failed':      ('失敗',        '#cf222e'),
    'cancelled':   ('中断',        '#bc4c00'),
}


class Tunnel:
    """1 風洞ぶんの状態とワーカー管理。"""
    def __init__(self, tunnel_id):
        self.id = tunnel_id
        self.name = f"風洞{tunnel_id}"
        self.fan_index = tunnel_id
        self.bound_usb_path = None        # 割当済みの物理ポート(固定)
        self.present_device_id = None     # 今そのポートに在る個体(無ければ None)
        self.miss_count = 0               # 連続不在カウンタ(抜線デバウンス用)
        self.status = 'free'
        self.detail = ''
        self.progress = 0.0
        self.worker = None
        self.cancel_event = threading.Event()

    @property
    def assigned(self):
        return self.bound_usb_path is not None

    @property
    def occupied(self):
        return self.present_device_id is not None

    @property
    def running(self):
        return self.worker is not None and self.worker.is_alive()


class CalibrationGUI:
    def __init__(self, root):
        self.root = root
        root.title("E-Sensor 風洞校正 (複数風洞)")
        self.tunnels = {tid: Tunnel(tid) for tid in TUNNEL_IDS}
        # 割当を拒否した(ダイアログでキャンセルした)ポートは、抜くまで再度聞かない。
        self._skip_ports = set()
        self._dialog_open = False
        self._build_ui()
        # USB監視は別スレッドで回し、結果を after で main へ反映する。
        self._stop = threading.Event()
        self._poller = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller.start()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- UI 構築 ----------------
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill='both', expand=True)

        header = ttk.Frame(top)
        header.pack(fill='x')
        ttk.Label(header, text="風洞", width=8, font=('', 10, 'bold')).grid(row=0, column=0)
        ttk.Label(header, text="状態", width=16, font=('', 10, 'bold')).grid(row=0, column=1)
        ttk.Label(header, text="個体(device_id)", width=16, font=('', 10, 'bold')).grid(row=0, column=2)
        ttk.Label(header, text="進捗 / 詳細", width=36, font=('', 10, 'bold')).grid(row=0, column=3)
        ttk.Label(header, text="操作", width=16, font=('', 10, 'bold')).grid(row=0, column=4)

        self.rows = {}
        for tid in TUNNEL_IDS:
            self.rows[tid] = self._build_row(top, tid)

        bar = ttk.Frame(top, padding=(0, 8, 0, 0))
        bar.pack(fill='x')
        self.status_line = ttk.Label(bar, text="USB監視中...", foreground='#57606a')
        self.status_line.pack(side='left')

    def _build_row(self, parent, tid):
        f = ttk.Frame(parent, padding=(0, 4))
        f.pack(fill='x')
        name = ttk.Label(f, text=self.tunnels[tid].name, width=8)
        name.grid(row=0, column=0, sticky='w')
        status = tk.Label(f, text='空き', width=16, anchor='w')
        status.grid(row=0, column=1, sticky='w')
        dev = ttk.Label(f, text='-', width=16)
        dev.grid(row=0, column=2, sticky='w')
        detail = ttk.Label(f, text='未割当', width=36, anchor='w')
        detail.grid(row=0, column=3, sticky='w')
        start_btn = ttk.Button(f, text='校正開始', width=8,
                               command=lambda t=tid: self._on_start(t))
        start_btn.grid(row=0, column=4, sticky='w')
        start_btn.state(['disabled'])
        result_btn = ttk.Button(f, text='結果', width=6,
                                command=lambda t=tid: self._open_result(t))
        result_btn.grid(row=0, column=5, sticky='w', padx=(4, 0))
        result_btn.state(['disabled'])
        return {'status': status, 'dev': dev, 'detail': detail,
                'start': start_btn, 'result': result_btn}

    # ---------------- USB 監視ループ(別スレッド) ----------------
    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                units = disc.list_usb_esensors()
            except Exception:
                units = []
            self.root.after(0, self._apply_units, units)
            self._stop.wait(POLL_INTERVAL_MS / 1000.0)

    def _apply_units(self, units):
        """メインスレッドで USB 状態を反映する。"""
        present_by_path = {u.usb_path: u for u in units}
        present_paths = set(present_by_path.keys())

        # 1) 割当済み風洞の在/不在を更新。抜線は連続不在を確認してから確定する
        #    (ハブは1台抜くと一瞬、他ポートも再列挙で消えるため。1周の取りこぼしで
        #    「抜かれた」＝校正中断、と誤判定しない)。
        for t in self.tunnels.values():
            if not t.assigned:
                continue
            if t.bound_usb_path in present_by_path:
                u = present_by_path[t.bound_usb_path]
                t.present_device_id = u.device_id
                t.miss_count = 0
                if t.status == 'free':
                    t.status, t.detail = 'ready', '準備完了。校正開始できます。'
            else:
                t.miss_count += 1
                if t.miss_count < REMOVE_DEBOUNCE_MISSES:
                    continue  # 一時的な取りこぼしとみなし現状維持
                # 連続不在が確定 → 抜線扱い(校正中なら中断し、空きへ。割当は保持)
                if t.running:
                    t.cancel_event.set()
                t.present_device_id = None
                if t.status != 'free':
                    t.status, t.detail = 'free', '(抜かれました)'

        # 2) どの風洞にも割り当たっていない新ポート → 割当ダイアログ
        bound_paths = {t.bound_usb_path for t in self.tunnels.values() if t.assigned}
        # 抜かれたポートは skip 対象から外す(再挿入時にまた聞けるように)
        self._skip_ports &= present_paths
        if not self._dialog_open:
            for path in present_paths:
                if path in bound_paths or path in self._skip_ports:
                    continue
                u = present_by_path[path]
                self._prompt_enroll(u)
                break  # 1周に1つずつ

        self._refresh_all()
        self.status_line.config(
            text=f"USB監視中: 接続 {len(units)} 台 / 割当済み "
                 f"{sum(1 for t in self.tunnels.values() if t.assigned)} 風洞")

    # ---------------- 割当ダイアログ ----------------
    def _prompt_enroll(self, unit):
        unassigned = [t for t in self.tunnels.values() if not t.assigned]
        if not unassigned:
            # 全風洞割当済み。以後この個体では聞かない。
            self._skip_ports.add(unit.usb_path)
            return

        self._dialog_open = True
        dlg = tk.Toplevel(self.root)
        dlg.title("風洞の割当")
        dlg.transient(self.root)
        dlg.grab_set()
        ttk.Label(dlg, padding=10,
                  text=f"新しいポートで E-Sensor (device_id={unit.device_id}, "
                       f"port {unit.port_addr}) を検出しました。\n"
                       f"どの風洞に割り当てますか？（以後このポートは固定されます）"
                  ).pack()
        sel = tk.IntVar(value=unassigned[0].id)
        body = ttk.Frame(dlg, padding=(10, 0))
        body.pack(fill='x')
        for t in unassigned:
            ttk.Radiobutton(body, text=f"{t.name} (fan{t.fan_index})",
                            variable=sel, value=t.id).pack(anchor='w')

        btns = ttk.Frame(dlg, padding=10)
        btns.pack(fill='x')

        def do_assign():
            t = self.tunnels[sel.get()]
            t.bound_usb_path = unit.usb_path
            t.present_device_id = unit.device_id
            t.status, t.detail = 'ready', '準備完了。校正開始できます。'
            self._close_dialog(dlg)
            self._refresh_all()

        def do_cancel():
            self._skip_ports.add(unit.usb_path)
            self._close_dialog(dlg)

        ttk.Button(btns, text="割り当てる", command=do_assign).pack(side='right')
        ttk.Button(btns, text="スキップ", command=do_cancel).pack(side='right', padx=(0, 6))
        dlg.protocol("WM_DELETE_WINDOW", do_cancel)

    def _close_dialog(self, dlg):
        self._dialog_open = False
        try:
            dlg.grab_release()
            dlg.destroy()
        except Exception:
            pass

    # ---------------- 校正開始 ----------------
    def _on_start(self, tid):
        t = self.tunnels[tid]
        if t.running or not t.occupied:
            return
        t.cancel_event.clear()
        t.status, t.detail, t.progress = 'verifying', '開始しています...', 0.0
        self._refresh_row(t)
        t.worker = threading.Thread(target=self._run_worker, args=(t,), daemon=True)
        t.worker.start()

    def _run_worker(self, t):
        device_id = t.present_device_id
        try:
            # 1) 対象個体の MIDI ペアを(開く直前に)再識別する
            self._ui(lambda: self._set(t, 'verifying', 'MIDIポート識別中...', 0.02))
            pair = disc.find_midi_pair(device_id)
            if pair is None:
                self._ui(lambda: self._set(t, 'failed', 'MIDI識別失敗(個体が見つからない)'))
                return
            if t.cancel_event.is_set():
                return

            # 2) 動作確認(verify_device)。FAIL でも校正は続行する。
            self._ui(lambda: self._set(t, 'verifying', '動作確認中...', 0.05))
            rc = verify_device.main(pair.in_name, pair.out_name)
            if t.cancel_event.is_set():
                self._ui(lambda: self._set(t, 'cancelled', '中断しました'))
                return
            verify_str = 'PASS' if rc == 0 else 'FAIL'

            # verify がポートを解放しきるまで少し待つ(単体版に倣う)
            time.sleep(0.5)

            # 3) 校正(この風洞の fan_index / プロファイル)
            cfg = CALIBRATOR_PROFILES[t.id]
            cal = AnemometerCalibrator(
                midi_in=pair.in_name, midi_out=pair.out_name,
                fan_index=t.fan_index,
                calibration_points=cfg['calibration_points'],
                validation_points=cfg['validation_points'],
                calibrator_id=t.id,
                on_progress=lambda msg, frac, tt=t: self._ui(
                    lambda: self._set(tt, 'calibrating', msg, frac)),
                on_abnormal=lambda v, tt=t: self._ask_abnormal(tt, v),
                should_cancel=t.cancel_event.is_set,
            )
            self._ui(lambda: self._set(t, 'calibrating', '校正中...', 0.1))
            ok = cal.run_calibration(show_plot=False)

            if t.cancel_event.is_set():
                self._ui(lambda: self._set(t, 'cancelled', '中断しました'))
            elif ok:
                def _done(tt=t, vs=verify_str):
                    self._set(tt, 'done', f'完了 (動作確認 {vs})', 1.0)
                    self._open_result(tt.id)   # 結果PNGを自動表示
                self._ui(_done)
            else:
                self._ui(lambda: self._set(t, 'failed',
                                           f'校正NG/中止 (動作確認 {verify_str})'))
        except Exception as e:
            self._ui(lambda: self._set(t, 'failed', f'例外: {e}'))
        finally:
            t.worker = None
            self._ui(lambda: self._refresh_row(t))

    def _ask_abnormal(self, t, voltage):
        """0m/s 異常電圧時の続行可否をメインスレッドのダイアログで尋ねる(ブロック)。"""
        result = {'v': False}
        done = threading.Event()

        def ask():
            result['v'] = messagebox.askyesno(
                f"{t.name}: 異常電圧",
                f"0 m/s 電圧が異常に低い ({voltage*1000:.1f} mV)。\n"
                f"風速計回路の不具合が疑われます。\n\nこのまま校正を続行しますか？")
            done.set()

        self.root.after(0, ask)
        done.wait()
        return result['v']

    # ---------------- 結果表示 ----------------
    def _open_result(self, tid):
        t = self.tunnels[tid]
        dev = t.present_device_id
        if not dev:
            return
        png = OUTPUT_DIR / f"plot_{dev}.png"
        if png.exists():
            try:
                os.startfile(str(png))  # Windows 既定ビューア
            except Exception as e:
                messagebox.showerror("結果表示", f"PNG を開けません: {e}")
        else:
            messagebox.showinfo("結果表示", f"まだ結果PNGがありません:\n{png}")

    # ---------------- UI 反映ヘルパ ----------------
    def _ui(self, fn):
        """ワーカースレッドから安全にUI更新をスケジュールする。"""
        self.root.after(0, fn)

    def _set(self, t, status, detail=None, progress=None):
        t.status = status
        if detail is not None:
            t.detail = detail
        if progress is not None:
            t.progress = progress
        self._refresh_row(t)

    def _refresh_all(self):
        for t in self.tunnels.values():
            self._refresh_row(t)

    def _refresh_row(self, t):
        r = self.rows[t.id]
        label, color = STATUS_STYLE.get(t.status, (t.status, '#000000'))
        r['status'].config(text=label, fg=color)
        r['dev'].config(text=t.present_device_id or '-')
        if not t.assigned:
            detail = '未割当'
        elif t.status in ('calibrating', 'verifying') and t.progress:
            detail = f"{t.detail}  [{int(t.progress*100)}%]"
        else:
            detail = t.detail
        r['detail'].config(text=detail)
        # 開始ボタン: 「新規接続直後(ready)かつ非実行」のときだけ有効。
        # done/failed/cancelled では無効のまま(誤って再校正しない)。抜線→再接続で
        # free→ready に戻ったときのみ再有効化される。
        if t.status == 'ready' and not t.running:
            r['start'].state(['!disabled'])
        else:
            r['start'].state(['disabled'])
        # 結果ボタン: 完了時に有効
        if t.status == 'done':
            r['result'].state(['!disabled'])
        else:
            r['result'].state(['disabled'])

    def _on_close(self):
        self._stop.set()
        for t in self.tunnels.values():
            t.cancel_event.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    CalibrationGUI(root)
    root.minsize(820, 200)
    root.mainloop()


if __name__ == '__main__':
    main()
