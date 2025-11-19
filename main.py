import gspread
from oauth2client.service_account import ServiceAccountCredentials
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.service import Service
from selenium.webdriver.edge.options import Options
from selenium.common.exceptions import WebDriverException, TimeoutException
from urllib3.exceptions import ReadTimeoutError
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import time
import re
import sys
import os
import json
import threading
import queue
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox

# --- Regex: chỉ lấy link voca.ro ---
VOCA_REGEX = re.compile(r"https?://voca(?:\.ro|roo\.com)/[A-Za-z0-9]+")

# --- Helper: Lấy đường dẫn file (cho PyInstaller) ---
def resource_path(relative_path):
    """ Lấy đường dẫn tuyệt đối đến tài nguyên, hoạt động cho cả dev và PyInstaller """
    try:
        # PyInstaller tạo ra folder tạm này và lưu đường dẫn vào _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    path = os.path.join(base_path, relative_path)
    
    # --- THÊM ĐOẠN NÀY ĐỂ DEBUG TRÊN MAC ---
    # Nếu file tồn tại, cấp quyền thực thi ngay lập tức (Fix lỗi binary not found/permission denied)
    if os.path.exists(path):
        try:
            os.chmod(path, 0o755) # Cấp quyền rwxr-xr-x
        except Exception:
            pass
            
    return path
    
# --- Helper: Chuyển tên cột sang số ---
def col_to_num(col_str):
    n = 0
    for c in col_str.upper():
        n = n * 26 + (ord(c) - ord('A')) + 1
    return n

# --- Selenium driver setup ---
def create_driver(driver_path):
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")
    service = Service(driver_path)
    driver = webdriver.Edge(service=service, options=options)
    driver.set_page_load_timeout(180)
    return driver

# --- Safe get with retry ---
def safe_get(driver, url, retries=2, delay=2):
    for attempt in range(retries + 1):
        try:
            start = time.time()
            driver.get(url)
            duration = time.time() - start
            return True
        except Exception as e:
            time.sleep(delay)
    return False

# --- Check one link ---
def check_link(driver, url, max_wait=40, interval=2):
    if not safe_get(driver, url):
        return "DRIVER_ERROR"
    elapsed = 0
    while elapsed < max_wait:
        try:
            if driver.find_elements(By.CSS_SELECTOR, ".Player"):
                return "OK"
            if driver.find_elements(By.CSS_SELECTOR, ".ContentBox.ContentBox--error"):
                return "ERROR_LINK"
            if driver.find_elements(By.CSS_SELECTOR, ".Recorder"):
                return "ERROR_LINK"
        except Exception:
            return "DRIVER_ERROR"
        time.sleep(interval)
        elapsed += interval
    return "TIMEDOUT"

# --- Worker xử lý nhiều link ---
def check_links_for_worker(tasks_for_worker, driver_path, log_queue):
    try:
        driver = create_driver(driver_path)
    except Exception as e:
        log_queue.put(f"[DRIVER_INIT_ERROR] Lỗi tạo driver: {e}")
        return [(row_idx, col_idx, "DRIVER_INIT_ERROR") for row_idx, col_idx, url in tasks_for_worker]
    
    results = []
    for row_idx, result_col, url in tasks_for_worker:
        log_queue.put(f"🧪 Checking row {row_idx}, col {result_col}") # Bỏ URL để log bớt rối
        try:
            status = check_link(driver, url)
        except Exception as e:
            log_queue.put(f"[ERROR] While checking link: {type(e).__name__}: {e}")
            try: driver.quit()
            except: pass
            try:
                driver = create_driver(driver_path)
                status = check_link(driver, url)
            except Exception as e2:
                log_queue.put(f"[FATAL] Retry failed: {type(e2).__name__}: {e2}")
                status = "DRIVER_ERROR"
        
        log_queue.put(f"[DONE] Row {row_idx}, Col {result_col} → {status}")
        results.append((row_idx, result_col, status))
        time.sleep(0.5)
        
    driver.quit()
    return results

# --- HÀM LOGIC CHÍNH (Chạy trong Thread) ---
def run_checker(config, log_queue, progress_queue):
    """
    Hàm logic chính, giao tiếp qua Queues.
    """
    
    def log(message):
        log_queue.put(str(message))

    def set_progress(percent):
        progress_queue.put(int(percent))

    try:
        # --- 1. Lấy và xử lý cấu hình ---
        log("🏁 Bắt đầu chạy...")
        set_progress(5)
        json_key_string = config["-JSON_CONTENT-"] 
        driver_path = resource_path(config["-DRIVER_FILE-"])
        
        # === THAY ĐỔI: Lấy số workers từ config ===
        try:
            max_workers = int(config["-MAX_WORKERS-"])
            if not 1 <= max_workers <= 20: # Giới hạn an toàn
                raise ValueError("Số workers phải từ 1 đến 20")
        except Exception as e:
            log(f"⚠️ Lỗi số workers: {e}. Đặt mặc định là 4.")
            max_workers = 4
        # ========================================

        link_cols = [col.strip().upper() for col in config["-LINK_COLS-"].split(',')]
        link_col_indexes = [col_to_num(col) for col in link_cols]
        result_start_index = col_to_num(config["-RESULT_COL-"].strip().upper())
        summary_col_index = result_start_index + len(link_cols)
        summary_col_letter = chr(64 + summary_col_index)
        max_col_needed = summary_col_index
        
    except Exception as e:
        log(f"❌ Lỗi cấu hình đầu vào: {e}")
        log_queue.put("---ERROR---") # Gửi tín hiệu Lỗi
        return

    # --- 2. Google Sheets setup ---
    try:
        log("🔑 Đang xác thực với nội dung JSON key...")
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        json_key_dict = json.loads(json_key_string) 
        creds = ServiceAccountCredentials.from_json_keyfile_dict(json_key_dict, scope) 
        client = gspread.authorize(creds)
        sheet = client.open_by_key(config["-SHEET_ID-"]).worksheet(config["-TAB_NAME-"])
        log(f"✅ Mở thành công Tab: {config['-TAB_NAME-']}")
        set_progress(10)
    except Exception as e:
        log(f"❌ Lỗi Google Sheets: {type(e).__name__}: {e}")
        log("Vui lòng kiểm tra lại Sheet ID, Tab Name, và nội dung JSON key.")
        log_queue.put("---ERROR---")
        return

    # --- 3. Đảm bảo đủ cột và Header ---
    try:
        log("📊 Kiểm tra và cập nhật Header...")
        if sheet.col_count < max_col_needed:
            sheet.add_cols(max_col_needed - sheet.col_count)
            log(f"    -> Đã thêm cột, tổng số cột mới: {max_col_needed}")

        header_names = [f"Status_{col}" for col in link_cols] + ["SUMMARY"]
        header_updates = []
        
        for offset, header_name in enumerate(header_names):
            col_to_update = result_start_index + offset
            header_updates.append(gspread.Cell(1, col_to_update, header_name))

        if header_updates:
            sheet.update_cells(header_updates, value_input_option='USER_ENTERED')
            log(f"    -> Đã cập nhật {len(header_updates)} header (từ cột {config['-RESULT_COL-']}).")
        set_progress(15)
            
    except Exception as e:
        log(f"❌ Lỗi khi cập nhật header: {e}")
        log_queue.put("---ERROR---")
        return

    # --- 4. Lấy dữ liệu và tạo tasks ---
    try:
        log("⏳ Đang đọc dữ liệu từ Sheet...")
        data = sheet.get_all_values()
        log(f"    -> Đã đọc {len(data) - 1} hàng.")
        
        all_updates = []
        tasks = []
        row_stats = {} 

        for row_idx, row in enumerate(data[1:], start=2):
            row_stats[row_idx] = [None] * len(link_col_indexes)
            for offset, link_col_idx in enumerate(link_col_indexes):
                result_col_idx = result_start_index + offset
                result_col_letter = chr(64 + result_col_idx)

                if link_col_idx - 1 >= len(row): continue
                cell_value = row[link_col_idx - 1].strip()
                if not cell_value: continue

                matches = VOCA_REGEX.findall(cell_value)
                if not matches:
                    all_updates.append({
                        "range": f"{result_col_letter}{row_idx}",
                        "values": [["INVALID_URL"]]
                    })
                    continue

                for url in matches:
                    tasks.append((row_idx, result_col_idx, url))

        log(f"    -> Đã tạo {len(tasks)} tasks kiểm tra link.")
        set_progress(20)
    except Exception as e:
        log(f"❌ Lỗi khi đọc dữ liệu Sheet: {e}")
        log_queue.put("---ERROR---")
        return

    # --- 5. Chia tasks và chạy đa luồng ---
    if not tasks:
        log("⚠️ Không tìm thấy link Vocaroo nào hợp lệ để kiểm tra.")
    else:
        # === THAY ĐỔI: Sử dụng biến max_workers ===
        chunk_size = (len(tasks) + max_workers - 1) // max_workers
        task_chunks = [tasks[i:i + chunk_size] for i in range(0, len(tasks), chunk_size)]
        
        log(f"🚀 Chạy {len(tasks)} tasks với {len(task_chunks)} workers (Giới hạn {max_workers})...")
        # =======================================
        
        cell_results = defaultdict(list)
        total_chunks = len(task_chunks)
        completed_chunks = 0
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(check_links_for_worker, chunk, driver_path, log_queue) for chunk in task_chunks]
            
            for future in as_completed(futures):
                completed_chunks += 1
                percent = 20 + int((completed_chunks / total_chunks) * 60) # Chạy từ 20% đến 80%
                set_progress(percent)
                
                try:
                    results = future.result()
                    for row_idx, col_idx, status in results:
                        cell_results[(row_idx, col_idx)].append(status)
                except Exception as e:
                    log(f"❌ Lỗi worker: {e}")

        # --- 6. Gộp kết quả ---
        log("✍️ Đang gộp kết quả...")
        for (row_idx, col_idx), statuses in cell_results.items():
            final_text = "; ".join(statuses)
            all_updates.append({
                "range": f"{chr(64 + col_idx)}{row_idx}",
                "values": [[final_text]]
            })
            stat_offset = col_idx - result_start_index
            if 0 <= stat_offset < len(row_stats[row_idx]):
                 row_stats[row_idx][stat_offset] = "OK" if all(s == "OK" for s in statuses) else "ERROR"

    # --- 7. Batch update STATUS ---
    log(f"📦 Chuẩn bị {len(all_updates)} cập nhật trạng thái...")
    set_progress(85)
    if all_updates:
        try:
            sheet.batch_update(all_updates, value_input_option='USER_ENTERED')
            log("    -> ✅ Đã batch update trạng thái link.")
        except Exception as e:
            log(f"    -> ❌ Lỗi khi batch update: {e}")

    # --- 8. Tạo và Batch update SUMMARY ---
    summary_updates = []
    for row_idx, statuses in row_stats.items():
        ok_count = sum(1 for s in statuses if s == "OK")
        total = sum(1 for s in statuses if s is not None)
        
        if total > 0:
            summary = f"{ok_count}/{total}"
            summary_updates.append({
                "range": f"{summary_col_letter}{row_idx}",
                "values": [[summary]]
            })
    
    log(f"🧾 Chuẩn bị {len(summary_updates)} cập nhật SUMMARY (Cột {summary_col_letter})...")
    set_progress(95)
    if summary_updates:
        try:
            sheet.batch_update(summary_updates, value_input_option='USER_ENTERED')
            log("    -> ✅ Đã batch update SUMMARY.")
        except Exception as e:
            log(f"    -> ❌ Lỗi khi batch update SUMMARY: {e}")
    else:
        log("    -> Không có gì để cập nhật SUMMARY.")

    log("\n🎉 --- HOÀN TẤT --- 🎉")
    set_progress(100)
    log_queue.put("---DONE---") # Gửi tín hiệu Hoàn tất

# --- LỚP GIAO DIỆN TKINTER ---
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Vocaroo Link Checker (v4 - Tkinter)")
        self.root.geometry("800x750") # <-- Tăng chiều cao một chút

        # Tạo các biến lưu trữ
        self.sheet_id_var = tk.StringVar(value="1x8-VMjDop-DHUxu7ESDsD5ndd1GM00MmG5Bq_382wHw")
        self.tab_name_var = tk.StringVar(value="PRACTICE SPEAKING 7")
        self.driver_path_var = tk.StringVar(value="msedgedriver")
        self.link_cols_var = tk.StringVar(value="E,F,G")
        self.result_col_var = tk.StringVar(value="H")
        
        # === THAY ĐỔI: Thêm biến cho số workers ===
        self.worker_count_var = tk.StringVar(value="4") 
        # ========================================

        self.json_content_default = """{
  "type": "service_account",
  "project_id": "spkcheck",
  "private_key_id": "...",
  "private_key": "...",
  "client_email": "...",
  "client_id": "...",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token",
  "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
  "client_x509_cert_url": "...",
  "universe_domain": "googleapis.com"
}"""
        
        # Tạo Queues
        self.log_queue = queue.Queue()
        self.progress_queue = queue.Queue()

        # Tạo widgets
        self.create_widgets()

        # Xử lý khi đóng cửa sổ
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        self.is_running = False

    def create_widgets(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # --- Frame Cấu hình ---
        config_frame = ttk.LabelFrame(main_frame, text="Cấu hình", padding="10")
        config_frame.pack(fill=tk.X, expand=True)
        
        # Grid layout cho cấu hình
        config_frame.columnconfigure(1, weight=1)

        ttk.Label(config_frame, text="Sheet ID:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        ttk.Entry(config_frame, textvariable=self.sheet_id_var).grid(row=0, column=1, sticky=tk.EW, padx=5, pady=5)
        
        ttk.Label(config_frame, text="Tab Name:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        ttk.Entry(config_frame, textvariable=self.tab_name_var).grid(row=1, column=1, sticky=tk.EW, padx=5, pady=5)

        ttk.Label(config_frame, text="Cột link (E,F,G):").grid(row=2, column=0, sticky=tk.W, padx=5, pady=5)
        ttk.Entry(config_frame, textvariable=self.link_cols_var).grid(row=2, column=1, sticky=tk.EW, padx=5, pady=5)

        ttk.Label(config_frame, text="Cột ghi KQ (H):").grid(row=3, column=0, sticky=tk.W, padx=5, pady=5)
        ttk.Entry(config_frame, textvariable=self.result_col_var).grid(row=3, column=1, sticky=tk.EW, padx=5, pady=5)
        
        # === THAY ĐỔI: Thêm ô chọn số workers ===
        ttk.Label(config_frame, text="Số workers (1-12):").grid(row=4, column=0, sticky=tk.W, padx=5, pady=5)
        # Dùng Spinbox để giới hạn lựa chọn
        worker_spinbox = ttk.Spinbox(config_frame, from_=1, to=12, textvariable=self.worker_count_var, width=5)
        worker_spinbox.grid(row=4, column=1, sticky=tk.W, padx=5, pady=5)
        # ========================================

        # Driver (thay đổi row thành 5)
        ttk.Label(config_frame, text="Driver File:").grid(row=5, column=0, sticky=tk.W, padx=5, pady=5)
        driver_frame = ttk.Frame(config_frame)
        driver_frame.grid(row=5, column=1, sticky=tk.EW)
        driver_frame.columnconfigure(0, weight=1)
        
        self.driver_entry = ttk.Entry(driver_frame, textvariable=self.driver_path_var)
        self.driver_entry.grid(row=0, column=0, sticky=tk.EW, padx=(0, 5))
        self.browse_button = ttk.Button(driver_frame, text="Browse...", command=self.browse_driver)
        self.browse_button.grid(row=0, column=1, sticky=tk.E)

        # JSON Content
        json_frame = ttk.LabelFrame(main_frame, text="Nội dung JSON Key", padding="10")
        json_frame.pack(fill=tk.X, expand=True, pady=10)
        self.json_text = tk.Text(json_frame, height=10, width=80)
        self.json_text.pack(fill=tk.BOTH, expand=True)
        self.json_text.insert(tk.END, self.json_content_default)

        # --- Frame Điều khiển ---
        control_frame = ttk.Frame(main_frame, padding="5")
        control_frame.pack(fill=tk.X, expand=True)
        control_frame.columnconfigure(0, weight=1)
        control_frame.columnconfigure(1, weight=1)

        self.start_button = ttk.Button(control_frame, text="Bắt đầu kiểm tra", command=self.start_checking, style="Accent.TButton")
        self.start_button.grid(row=0, column=0, padx=5, sticky=tk.EW)

        self.cancel_button = ttk.Button(control_frame, text="Hủy (Đóng App)", command=self.on_close, state=tk.DISABLED)
        self.cancel_button.grid(row=0, column=1, padx=5, sticky=tk.EW)
        
        # Progress Bar
        self.progress_bar = ttk.Progressbar(main_frame, orient=tk.HORIZONTAL, length=100, mode='determinate')
        self.progress_bar.pack(fill=tk.X, expand=True, pady=10)

        # --- Frame Log ---
        log_frame = ttk.LabelFrame(main_frame, text="Log", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True)
        
        self.log_area = scrolledtext.ScrolledText(log_frame, state='disabled', height=15)
        self.log_area.pack(fill=tk.BOTH, expand=True)

    def browse_driver(self):
        filename = filedialog.askopenfilename(title="Chọn file msedgedriver")
        if filename:
            self.driver_path_var.set(filename)

    def start_checking(self):
        # Lấy config
        config = {
            "-SHEET_ID-": self.sheet_id_var.get(),
            "-TAB_NAME-": self.tab_name_var.get(),
            "-DRIVER_FILE-": self.driver_path_var.get(),
            "-LINK_COLS-": self.link_cols_var.get(),
            "-RESULT_COL-": self.result_col_var.get(),
            "-JSON_CONTENT-": self.json_text.get("1.0", tk.END),
            # === THAY ĐỔI: Thêm số workers vào config ===
            "-MAX_WORKERS-": self.worker_count_var.get(),
            # ==========================================
        }

        # Vô hiệu hóa nút
        self.start_button.config(state=tk.DISABLED)
        self.cancel_button.config(state=tk.NORMAL)
        self.is_running = True

        # Xóa log cũ, reset progress
        self.log_area.config(state='normal')
        self.log_area.delete('1.0', tk.END)
        self.log_area.config(state='disabled')
        self.progress_bar['value'] = 0

        # Chạy logic trong 1 thread riêng
        self.worker_thread = threading.Thread(
            target=run_checker, 
            args=(config, self.log_queue, self.progress_queue),
            daemon=True 
        )
        self.worker_thread.start()

        # Bắt đầu lắng nghe queue
        self.root.after(100, self.process_queues)

    def process_queues(self):
        # Xử lý log
        try:
            while True:
                msg = self.log_queue.get_nowait()
                
                if msg == "---DONE---":
                    self.on_checking_done()
                    return # Dừng vòng lặp
                elif msg == "---ERROR---":
                    self.on_checking_error()
                    return # Dừng vòng lặp

                self.log_area.config(state='normal')
                self.log_area.insert(tk.END, msg + '\n')
                self.log_area.see(tk.END) # Cuộn xuống cuối
                self.log_area.config(state='disabled')
                
        except queue.Empty:
            pass # Không có log mới

        # Xử lý progress
        try:
            while True:
                percent = self.progress_queue.get_nowait()
                self.progress_bar['value'] = percent
        except queue.Empty:
            pass # Không có progress mới

        # Tiếp tục lắng nghe nếu thread còn chạy
        if self.worker_thread.is_alive():
            self.root.after(100, self.process_queues)
        else:
            if self.is_running:
                self.on_checking_error("Thread bị dừng đột ngột.")

    def on_checking_done(self):
        self.is_running = False
        self.start_button.config(state=tk.NORMAL)
        self.cancel_button.config(state=tk.DISABLED)
        self.progress_bar['value'] = 100
        messagebox.showinfo("Hoàn tất", "Đã kiểm tra và cập nhật xong!")

    def on_checking_error(self, custom_msg=None):
        self.is_running = False
        self.start_button.config(state=tk.NORMAL)
        self.cancel_button.config(state=tk.DISABLED)
        
        if custom_msg:
            self.log_area.config(state='normal')
            self.log_area.insert(tk.END, f"❌ LỖI: {custom_msg}\n")
            self.log_area.see(tk.END)
            self.log_area.config(state='disabled')

        messagebox.showerror("Lỗi", "Có lỗi xảy ra. Vui lòng xem Log.")

    def on_close(self):
        if self.is_running:
            if messagebox.askyesno("Đang chạy...", "Ứng dụng đang kiểm tra link. Bạn có chắc muốn HỦY và thoát?"):
                self.root.destroy()
                sys.exit(0)
        else:
            self.root.destroy()

# --- Entry Point ---
if __name__ == "__main__":
    try:
        from ttkthemes import ThemedTk
        root = ThemedTk(theme="arc")
    except ImportError:
        print("Không tìm thấy ttkthemes, dùng theme mặc định.")
        root = tk.Tk()
        
        style = ttk.Style()
        # Đảm bảo style "Accent.TButton" tồn tại
        style.configure("Accent.TButton", foreground="white", background="#0078D4")


    app = App(root)
    root.mainloop()
