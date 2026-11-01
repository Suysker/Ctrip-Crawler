import magic
import io
import os
import gzip
import time
import json
import pandas as pd
from seleniumwire import webdriver
from datetime import datetime as dt, timedelta
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
import threading

# 爬取的城市
crawl_citys = ["上海", "香港", "东京"]

# 爬取日期范围：起始日期。格式'2023-12-01'
begin_date = None

# 爬取日期范围：结束日期。格式'2023-12-31'
end_date = None

# 爬取T+N，即N天后
start_interval = 1

# 爬取的日期
crawl_days = 60

# 设置各城市爬取的时间间隔（单位：秒）
crawl_interval = 5

# 日期间隔
days_interval = 1

# 设置页面加载的最长等待时间（单位：秒）
max_wait_time = 10

# 最大错误重试次数
max_retry_time = 5

# 是否只抓取直飞信息（True: 只抓取直飞，False: 抓取所有航班）
direct_flight = True

# 是否抓取航班舒适信息（True: 抓取，False: 不抓取）
comft_flight = False

# 是否删除不重要的信息
del_info = False

# 是否重命名DataFrame的列名
rename_col = True

# 调试截图
enable_screenshot = False

# 允许登录（可能必须要登录才能获取数据）
login_allowed = True

# 账号
accounts = ['','']

# 密码
passwords = ['','']

#本地登录缓存
COOKIES_FILE = "cookies.json"
REQUIRED_COOKIES = ["AHeadUserInfo", "DUID", "IsNonUser", "_udl", "cticket", "login_type", "login_uid"]

def init_driver():
    # options = webdriver.ChromeOptions() # 创建一个配置对象
    options = webdriver.EdgeOptions()  # 创建一个配置对象
    options.add_argument("--incognito")  # 隐身模式（无痕模式）
    # options.add_argument('--headless')  # 启用无头模式
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--pageLoadStrategy=eager")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-certificate-errors-spki-list")
    options.add_argument("--ignore-ssl-errors")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])  # 不显示正在受自动化软件控制的提示
    # 如果需要指定Chrome驱动的路径，取消下面这行的注释并设置正确的路径
    # chromedriver_path = '/path/to/chromedriver'
    # 如果需要指定路径，可以加上executable_path参数
    # driver = webdriver.Chrome(options=options)  
    driver = webdriver.Edge(options=options)
    driver.maximize_window()

    return driver

def gen_citys(crawl_citys):
    # 生成城市组合表
    citys = []
    ytic = list(reversed(crawl_citys))
    for m in crawl_citys:
        for n in ytic:
            if m == n:
                continue
            else:
                citys.append([m, n])
    return citys

def generate_flight_dates(n, begin_date, end_date, start_interval, days_interval):
    flight_dates = []
    
    if begin_date:
        begin_date = dt.strptime(begin_date, "%Y-%m-%d")
    elif start_interval:
        begin_date = dt.now() + timedelta(days=start_interval)
        
    for i in range(0, n, days_interval):
        flight_date = begin_date + timedelta(days=i)

        flight_dates.append(flight_date.strftime("%Y-%m-%d"))
    
    # 如果有结束日期，确保生成的日期不超过结束日期
    if end_date:
        end_date = dt.strptime(end_date, "%Y-%m-%d")
        flight_dates = [date for date in flight_dates if dt.strptime(date, "%Y-%m-%d") <= end_date]
        # 继续生成日期直到达到或超过结束日期
        while dt.strptime(flight_dates[-1], "%Y-%m-%d") < end_date:
            next_date = dt.strptime(flight_dates[-1], "%Y-%m-%d") + timedelta(days=days_interval)
            if next_date <= end_date:
                flight_dates.append(next_date.strftime("%Y-%m-%d"))
            else:
                break
    
    return flight_dates

# element_to_be_clickable 函数来替代 expected_conditions.element_to_be_clickable 或 expected_conditions.visibility_of_element_located
def element_to_be_clickable(element):
    def check_clickable(driver):
        try:
            if element.is_enabled() and element.is_displayed():
                return element  # 当条件满足时，返回元素本身
            else:
                return False
        except:
            return False

    return check_clickable

class DataFetcher(object):
    def __init__(self, driver):
        self.driver = driver
        self.date = None
        self.city = None
        self.err = 0  # 错误重试次数
        self.switch_acc = 0 #切换账户
        self.comfort_data = None  # 航班舒适度信息

    def refresh_driver(self):
        try:
            self.driver.refresh()
        except Exception as e:
            # 错误次数+1
            self.err += 1

            print(
                f'{time.strftime("%Y-%m-%d_%H-%M-%S")} refresh_driver:刷新页面失败，错误类型：{type(e).__name__}, 详细错误信息：{str(e).split("Stacktrace:")[0]}'
            )
            
            # 保存错误截图
            if enable_screenshot:
                self.driver.save_screenshot(
                    f'screenshot/screenshot_{time.strftime("%Y-%m-%d_%H-%M-%S")}.png'
                )
            if self.err < max_retry_time:
                # 刷新页面
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} refresh_driver：刷新页面')
                self.refresh_driver()

            # 判断错误次数
            if self.err >= max_retry_time:
                print(
                    f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 错误次数【{self.err}-{max_retry_time}】,refresh_driver:不继续重试'
                )
    
    def remove_btn(self):
        try:
            #WebDriverWait(self.driver, max_wait_time).until(lambda d: d.execute_script('return typeof jQuery !== "undefined"'))
            # 移除提醒
            self.driver.execute_script("document.querySelectorAll('.notice-box').forEach(element => element.remove());")
            # 移除在线客服
            self.driver.execute_script("document.querySelectorAll('.shortcut, .shortcut-link').forEach(element => element.remove());")
            # 移除分享链接
            self.driver.execute_script("document.querySelectorAll('.shareline').forEach(element => element.remove());")
            '''
            # 使用JavaScript删除有的<dl>标签
            self.driver.execute_script("""
                var elements = document.getElementsByTagName('dl');
                while(elements.length > 0){
                    elements[0].parentNode.removeChild(elements[0]);
                }
            """)
            '''
        except Exception as e:
            print(
                f'{time.strftime("%Y-%m-%d_%H-%M-%S")} remove_btn:提醒移除失败，错误类型：{type(e).__name__}, 详细错误信息：{str(e).split("Stacktrace:")[0]}'
            )

    def check_verification_code(self):
        try:
            # 检查是否有验证码元素，如果有，则需要人工处理
            if (len(self.driver.find_elements(By.ID, "verification-code")) + len(self.driver.find_elements(By.CLASS_NAME, "alert-title"))):
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} check_verification_code：验证码被触发verification-code/alert-title，请手动完成验证。')
    
                user_input_completed = threading.Event()
                # 等待用户手动处理验证码
                def wait_for_input():
                    input("请完成验证码，然后按回车键继续...")
                    user_input_completed.set()
    
                input_thread = threading.Thread(target=wait_for_input)
                input_thread.start()
    
                # 设置手动验证超时时间
                timeout_seconds = crawl_interval * 100
    
                input_thread.join(timeout=timeout_seconds)
    
                if user_input_completed.is_set():
                    print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} check_verification_code：验证码处理完成，继续执行。')
    
                    # 等待页面加载完成
                    WebDriverWait(self.driver, max_wait_time).until(
                        EC.presence_of_element_located((By.CLASS_NAME, "pc_home-jipiao"))
                    )
                    
                    # 移除注意事项
                    self.remove_btn()
                    return True
                else:
                    print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} check_verification_code: 手动验证超时 {timeout_seconds} 秒')
                    self.driver.quit()
                    self.driver = init_driver()
                    self.err = 0
                    self.switch_acc += 1
                    self.get_page(1)
                    return False
            else:
                # 移除注意事项
                self.remove_btn()
                # 如果没有找到验证码元素，则说明页面加载成功，没有触发验证码
                return True
        except Exception as e:
            print(
                f'{time.strftime("%Y-%m-%d_%H-%M-%S")} check_verification_code:未知错误，错误类型：{type(e).__name__}, 详细错误信息：{str(e).split("Stacktrace:")[0]}'
            )
            return False

    def load_cookies(self, account):
        if os.path.exists(COOKIES_FILE):
            try:
                with open(COOKIES_FILE, "r") as f:
                    cookies_all = json.load(f)
                return cookies_all.get(account)
            except Exception as e:
                print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} load_cookies: 读取 cookies 出错：{e}")
                return None
        return None

    def save_cookies(self, account, cookies):
        cookies_all = {}
        if os.path.exists(COOKIES_FILE):
            try:
                with open(COOKIES_FILE, "r") as f:
                    cookies_all = json.load(f)
            except Exception:
                cookies_all = {}
        cookies_all[account] = cookies
        with open(COOKIES_FILE, "w") as f:
            json.dump(cookies_all, f)

    def delete_cookies(self, account):
        try:
            if os.path.exists(COOKIES_FILE):
                with open(COOKIES_FILE, "r") as f:
                    cookies_all = json.load(f)
                if account in cookies_all:
                    del cookies_all[account]
                    with open(COOKIES_FILE, "w") as f:
                        json.dump(cookies_all, f)
                    print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} login: 成功删除账号 {account} 的 cookies")
        except Exception as e:
            print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} login: 删除账号 {account} cookies 失败：{e}")

    def login(self):
        if login_allowed:
            
            account = accounts[self.switch_acc % len(accounts)]
            password = passwords[self.switch_acc % len(passwords)]
            
            # ===== 尝试使用本地缓存的 cookies 登录 =====
            local_cookies = self.load_cookies(account)
            if local_cookies:
                print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} login: 检测到本地 cookies，尝试通过 cookies 登录")
                for cookie in local_cookies:
                    try:
                        self.driver.add_cookie(cookie)
                    except Exception as e:
                        print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} login: 添加 cookie {cookie.get('name')} 失败：{e}")
                
                try:
                    # 检测登录状态，通过https://my.ctrip.com/myinfo/home
                    self.driver.get('https://my.ctrip.com/myinfo/home')
                    
                    WebDriverWait(self.driver, max_wait_time).until(
                        lambda d: d.current_url == 'https://my.ctrip.com/myinfo/home'
                    )
                    print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} login: 已通过 cookie 登录")
                    self.err += 99
                    return 1
                except Exception:
                    print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} 错误次数【{self.err}-{max_retry_time}】 login: cookie 登录失效，重新走登录流程")
                    self.err += 1
                    if self.err >= max_retry_time:
                        print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} login: cookie 登录失败次数超过 {max_retry_time} 次，删除该账号 cookies")
                        self.delete_cookies(account)
                        self.err = 0
                    self.login()
            else:
                try:
                    if len(self.driver.find_elements(By.CLASS_NAME, "lg_loginbox_modal")) == 0:
                        print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login:未弹出登录界面')
                        WebDriverWait(self.driver, max_wait_time).until(EC.presence_of_element_located((By.CLASS_NAME, "tl_nfes_home_header_login_wrapper_siwkn")))
                        # 点击飞机图标，返回主界面
                        ele = WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_element(By.CLASS_NAME, "tl_nfes_home_header_login_wrapper_siwkn")))
                        ele.click()
                        #等待页面加载
                        WebDriverWait(self.driver, max_wait_time).until(EC.presence_of_element_located((By.CLASS_NAME, "lg_loginwrap")))
                    else:
                        print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login:已经弹出登录界面')
                    
                    ele = WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_elements(By.CLASS_NAME, "r_input.bbz-js-iconable-input")[0]))
                    ele.send_keys(account)
                    print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login:输入账户成功')
                    
                    ele = WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_element(By.CSS_SELECTOR, "div[data-testid='accountPanel'] input[data-testid='passwordInput']")))
                    ele.send_keys(password)
                    print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login:输入密码成功')
                    
                    ele = WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_element(By.CSS_SELECTOR, '[for="checkboxAgreementInput"]')))
                    ele.click()
                    print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login:勾选同意成功')
                    
                    ele = WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_elements(By.CLASS_NAME, "form_btn.form_btn--block")[0]))
                    ele.click()
    
                    # 检查是否出现验证码验证页面（max_wait_time秒内检测）
                    try:
                        WebDriverWait(self.driver, max_wait_time).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='doubleAuthSwitcherBox']"))
                        )
                        print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login: 检测到验证码验证页面')
                        
                        # 定义验证码弹窗的父级选择器
                        double_auth_selector = "[data-testid='doubleAuthSwitcherBox']"
                        
                        # 从 doubleAuthSwitcherBox 内定位发送验证码按钮并点击
                        send_btn = WebDriverWait(self.driver, max_wait_time).until(
                            EC.element_to_be_clickable((By.CSS_SELECTOR, f"{double_auth_selector} dl[data-testid='dynamicCodeInput'] a.btn-primary-s"))
                        )
                        send_btn.click()
                        print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login: 发送验证码按钮点击')
                        
                        # 使用线程等待用户在控制台输入验证码，超时则按超时处理逻辑
                        verification_code = [None]
                        user_input_completed = threading.Event()
                        
                        def wait_for_verification_input():
                            verification_code[0] = input("请输入收到的验证码: ")
                            user_input_completed.set()
                        
                        input_thread = threading.Thread(target=wait_for_verification_input)
                        input_thread.start()
                        timeout_seconds = crawl_interval * 100
                        input_thread.join(timeout=timeout_seconds)
                        
                        if not user_input_completed.is_set():
                            print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login: 验证码输入超时 {timeout_seconds} 秒')
                            self.switch_acc += 1
                            self.err += 99
                            return 0
                        
                        # 从 doubleAuthSwitcherBox 内定位验证码输入框，并输入验证码
                        code_input = WebDriverWait(self.driver, max_wait_time).until(
                            EC.element_to_be_clickable((By.CSS_SELECTOR, f"{double_auth_selector} input[data-testid='verifyCodeInput']"))
                        )
                        code_input.send_keys(verification_code)
                        print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login: 验证码输入成功')
                        
                        # 从 doubleAuthSwitcherBox 内定位并点击“验 证”按钮
                        verify_btn = WebDriverWait(self.driver, max_wait_time).until(
                            EC.element_to_be_clickable((By.CSS_SELECTOR, f"{double_auth_selector} dl[data-testid='dynamicVerifyButton'] input[type='submit']"))
                        )
                        verify_btn.click()
                        print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login: 验证码提交成功')
                        
                        # 等待验证码验证后的页面加载，比如首页的某个关键元素
                        WebDriverWait(self.driver, max_wait_time).until(
                            EC.presence_of_element_located((By.CLASS_NAME, "pc_home-jipiao"))
                        )
                    except Exception as e:
                        # 如果max_wait_time秒内未检测到验证码页面，则认为是正常登录流程
                        print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login: 未检测到验证码验证页面，继续执行')
                    
                    print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login：登录成功')
                    # 保存登录截图
                    if enable_screenshot:
                        self.driver.save_screenshot(
                            f'screenshot/screenshot_{time.strftime("%Y-%m-%d_%H-%M-%S")}.png'
                        )
                    time.sleep(crawl_interval*3)
                    
                    # ===== 登录成功后提取需要的 cookies 并保存 =====
                    all_cookies = self.driver.get_cookies()
                    filtered_cookies = [ck for ck in all_cookies if ck.get("name") in REQUIRED_COOKIES]
                    self.save_cookies(account, filtered_cookies)
                    print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login: cookies 已保存')
                    
                except Exception as e:
                    # 错误次数+1
                    self.err += 1
                    # 用f字符串格式化错误类型和错误信息，提供更多的调试信息
                    print(
                        f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login：页面加载或元素操作失败，错误类型：{type(e).__name__}, 详细错误信息：{str(e).split("Stacktrace:")[0]}'
                    )
        
                    # 保存错误截图
                    if enable_screenshot:
                        self.driver.save_screenshot(
                            f'screenshot/screenshot_{time.strftime("%Y-%m-%d_%H-%M-%S")}.png'
                        )
                        
                    if self.err < max_retry_time:
                        # 刷新页面
                        print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} login：刷新页面')
                        self.refresh_driver()
                        # 检查注意事项和验证码
                        if self.check_verification_code():
                            # 重试
                            self.login()
                    # 判断错误次数
                    if self.err >= max_retry_time:
                        print(
                            f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 错误次数【{self.err}-{max_retry_time}】,login:重新尝试加载页面，这次指定需要重定向到首页'
                        )

    def get_page(self, reset_to_homepage=0):
        next_stage_flag = False
        try:
            if reset_to_homepage == 1:
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 尝试前往首页...')
                start_time = time.time()
                # 前往首页
                self.driver.get(
                    "https://flights.ctrip.com/online/channel/domestic")
                end_time = time.time()
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 前往首页耗时: {end_time - start_time:.2f} 秒')

            print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 当前页面 URL: {self.driver.current_url}')
            print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 当前页面标题: {self.driver.title}')

            # 检查注意事项和验证码
            if self.check_verification_code():
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 等待页面加载完成...')
                WebDriverWait(self.driver, max_wait_time).until(
                    EC.presence_of_element_located(
                        (By.CLASS_NAME, "pc_home-jipiao"))
                )
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 页面加载完成')

                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 尝试点击飞机图标...')
                # 点击飞机图标，返回主界面
                ele = WebDriverWait(self.driver, max_wait_time).until(
                    element_to_be_clickable(
                        self.driver.find_element(
                            By.CLASS_NAME, "pc_home-jipiao")
                    )
                )
                ele.click()
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 成功点击飞机图标')

                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 尝试选择单程...')
                # 单程
                ele = WebDriverWait(self.driver, max_wait_time).until(
                    element_to_be_clickable(
                        self.driver.find_elements(
                            By.CLASS_NAME, "radio-label")[0]
                    )
                )
                ele.click()
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 成功选择单程')

                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 尝试点击搜索按钮...')
                # 搜索
                ele = WebDriverWait(self.driver, max_wait_time).until(
                    element_to_be_clickable(
                        self.driver.find_element(By.CLASS_NAME, "search-btn")
                    )
                )
                ele.click()
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 成功点击搜索按钮')

                next_stage_flag = True
        except Exception as e:
            # 用f字符串格式化错误类型和错误信息，提供更多的调试信息
            print(
                f'{time.strftime("%Y-%m-%d_%H-%M-%S")} get_page：页面加载或元素操作失败，错误类型：{type(e).__name__}, 详细错误信息：{str(e).split("Stacktrace:")[0]}'
            )
            print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 当前页面 URL: {self.driver.current_url}')
            print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 当前页面标题: {self.driver.title}')
            print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 当前页面源代码: {self.driver.page_source[:500]}...')  # 只打印前500个字符

            # 保存错误截图
            if enable_screenshot:
                screenshot_path = f'screenshot/screenshot_{time.strftime("%Y-%m-%d_%H-%M-%S")}.png'
                self.driver.save_screenshot(screenshot_path)
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 错误截图已保存: {screenshot_path}')

            # 重新尝试加载页面，这次指定需要重定向到首页
            print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 重新尝试加载页面，这次指定需要重定向到首页')
            self.get_page(1)
        else:
            if next_stage_flag:
                # 继续下一步
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 页面加载成功，继续下一步')
                self.change_city()
            else:
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 页面加载成功，但未能完成所有操作')

    def change_city(self):
        next_stage_flag = False
        try:
            # 等待页面完成加载
            WebDriverWait(self.driver, max_wait_time).until(
                EC.presence_of_element_located(
                    (By.CLASS_NAME, "form-input-v3"))
            )

            # 检查注意事项和验证码
            if self.check_verification_code():
                # 若出发地与目标值不符，则更改出发地
                while self.city[0] not in self.driver.find_elements(
                    By.CLASS_NAME, "form-input-v3"
                )[0].get_attribute("value"):
                    ele = WebDriverWait(self.driver, max_wait_time).until(
                        element_to_be_clickable(
                            self.driver.find_elements(
                                By.CLASS_NAME, "form-input-v3")[0]
                        )
                    )
                    ele.click()
                    ele = WebDriverWait(self.driver, max_wait_time).until(
                        element_to_be_clickable(
                            self.driver.find_elements(
                                By.CLASS_NAME, "form-input-v3")[0]
                        )
                    )
                    ele.send_keys(Keys.CONTROL + "a")
                    ele = WebDriverWait(self.driver, max_wait_time).until(
                        element_to_be_clickable(
                            self.driver.find_elements(
                                By.CLASS_NAME, "form-input-v3")[0]
                        )
                    )
                    ele.send_keys(self.city[0])

                print(
                    f'{time.strftime("%Y-%m-%d_%H-%M-%S")} change_city：更换城市【0】-{self.driver.find_elements(By.CLASS_NAME,"form-input-v3")[0].get_attribute("value")}'
                )

                # 若目的地与目标值不符，则更改目的地
                while self.city[1] not in self.driver.find_elements(
                    By.CLASS_NAME, "form-input-v3"
                )[1].get_attribute("value"):
                    ele = WebDriverWait(self.driver, max_wait_time).until(
                        element_to_be_clickable(
                            self.driver.find_elements(
                                By.CLASS_NAME, "form-input-v3")[1]
                        )
                    )
                    ele.click()
                    ele = WebDriverWait(self.driver, max_wait_time).until(
                        element_to_be_clickable(
                            self.driver.find_elements(
                                By.CLASS_NAME, "form-input-v3")[1]
                        )
                    )
                    ele.send_keys(Keys.CONTROL + "a")
                    ele = WebDriverWait(self.driver, max_wait_time).until(
                        element_to_be_clickable(
                            self.driver.find_elements(
                                By.CLASS_NAME, "form-input-v3")[1]
                        )
                    )
                    ele.send_keys(self.city[1])

                print(
                    f'{time.strftime("%Y-%m-%d_%H-%M-%S")} change_city：更换城市【1】-{self.driver.find_elements(By.CLASS_NAME,"form-input-v3")[1].get_attribute("value")}'
                )

                while (
                    self.driver.find_elements(By.CSS_SELECTOR, "[aria-label=请选择日期]")[
                        0
                    ].get_attribute("value")
                    != self.date
                ):
                    # 点击日期选择
                    ele = WebDriverWait(self.driver, max_wait_time).until(
                        element_to_be_clickable(
                            self.driver.find_element(
                                By.CLASS_NAME, "modifyDate.depart-date"
                            )
                        )
                    )
                    ele.click()

                    if int(
                        self.driver.find_elements(
                            By.CLASS_NAME, "date-picker.date-picker-block"
                        )[1]
                        .find_element(By.CLASS_NAME, "year")
                        .text[:-1]
                    ) < int(self.date[:4]):
                        ele = WebDriverWait(self.driver, max_wait_time).until(
                            element_to_be_clickable(
                                self.driver.find_elements(
                                    By.CLASS_NAME,
                                    "in-date-picker.icon.next-ico.iconf-right",
                                )[1]
                            )
                        )
                        print(
                            f'{time.strftime("%Y-%m-%d_%H-%M-%S")} change_city：更换日期{int(self.driver.find_elements(By.CLASS_NAME, "date-picker.date-picker-block")[1].find_element(By.CLASS_NAME, "year").text[:-1])}小于 {int(self.date[:4])} 向右点击'
                        )
                        ele.click()
                        
                    if int(
                        self.driver.find_elements(
                            By.CLASS_NAME, "date-picker.date-picker-block"
                        )[0]
                        .find_element(By.CLASS_NAME, "year")
                        .text[:-1]
                    ) > int(self.date[:4]):
                        ele = WebDriverWait(self.driver, max_wait_time).until(
                            element_to_be_clickable(
                                self.driver.find_elements(
                                    By.CLASS_NAME,
                                    "in-date-picker.icon.prev-ico.iconf-left",
                                )[0]
                            )
                        )
                        print(
                            f'{time.strftime("%Y-%m-%d_%H-%M-%S")} change_city：更换日期{int(self.driver.find_elements(By.CLASS_NAME, "date-picker.date-picker-block")[0].find_element(By.CLASS_NAME, "year").text[:-1])}大于 {int(self.date[:4])} 向左点击'
                        )
                        ele.click()

                    if int(
                        self.driver.find_elements(
                            By.CLASS_NAME, "date-picker.date-picker-block"
                        )[0]
                        .find_element(By.CLASS_NAME, "year")
                        .text[:-1]
                    ) == int(self.date[:4]):
                        if int(
                            self.driver.find_elements(
                                By.CLASS_NAME, "date-picker.date-picker-block"
                            )[0]
                            .find_element(By.CLASS_NAME, "month")
                            .text[:-1]
                        ) > int(self.date[5:7]):
                            ele = WebDriverWait(self.driver, max_wait_time).until(
                                element_to_be_clickable(
                                    self.driver.find_elements(
                                        By.CLASS_NAME,
                                        "in-date-picker.icon.prev-ico.iconf-left",
                                    )[0]
                                )
                            )
                            print(
                                f'{time.strftime("%Y-%m-%d_%H-%M-%S")} change_city：更换日期{int(self.driver.find_elements(By.CLASS_NAME, "date-picker.date-picker-block")[0].find_element(By.CLASS_NAME, "month").text[:-1])}大于 {int(self.date[5:7])} 向左点击'
                            )
                            ele.click()
                            
                    if int(
                        self.driver.find_elements(
                            By.CLASS_NAME, "date-picker.date-picker-block"
                        )[1]
                        .find_element(By.CLASS_NAME, "year")
                        .text[:-1]
                    ) == int(self.date[:4]):
                        if int(
                            self.driver.find_elements(
                                By.CLASS_NAME, "date-picker.date-picker-block"
                            )[1]
                            .find_element(By.CLASS_NAME, "month")
                            .text[:-1]
                        ) < int(self.date[5:7]):
                            ele = WebDriverWait(self.driver, max_wait_time).until(
                                element_to_be_clickable(
                                    self.driver.find_elements(
                                        By.CLASS_NAME,
                                        "in-date-picker.icon.next-ico.iconf-right",
                                    )[1]
                                )
                            )
                            print(
                                f'{time.strftime("%Y-%m-%d_%H-%M-%S")} change_city：更换日期{int(self.driver.find_elements(By.CLASS_NAME, "date-picker.date-picker-block")[1].find_element(By.CLASS_NAME, "month").text[:-1])}小于 {int(self.date[5:7])} 向右点击'
                            )
                            ele.click()

                    for m in self.driver.find_elements(
                        By.CLASS_NAME, "date-picker.date-picker-block"
                    ):
                        if int(m.find_element(By.CLASS_NAME, "year").text[:-1]) != int(
                            self.date[:4]
                        ):
                            continue

                        if int(m.find_element(By.CLASS_NAME, "month").text[:-1]) != int(
                            self.date[5:7]
                        ):
                            continue

                        for d in m.find_elements(By.CLASS_NAME, "date-d"):
                            if int(d.text) == int(self.date[-2:]):
                                ele = WebDriverWait(self.driver, max_wait_time).until(
                                    element_to_be_clickable(d)
                                )
                                ele.click()
                                break
                print(
                    f'{time.strftime("%Y-%m-%d_%H-%M-%S")} change_city：更换日期-{self.driver.find_elements(By.CSS_SELECTOR,"[aria-label=请选择日期]")[0].get_attribute("value")}'
                )

                while "(" not in self.driver.find_elements(
                    By.CLASS_NAME, "form-input-v3"
                )[0].get_attribute("value"):
                    # Enter搜索
                    # ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(its[1]))
                    # ele.send_keys(Keys.ENTER)
                    ele = WebDriverWait(self.driver, max_wait_time).until(
                        element_to_be_clickable(
                            self.driver.find_elements(
                                By.CLASS_NAME, "form-input-v3")[0]
                        )
                    )
                    ele.click()

                    # 通过低价提醒按钮实现enter键换页
                    ele = WebDriverWait(self.driver, max_wait_time).until(
                        element_to_be_clickable(
                            self.driver.find_elements(
                                By.CLASS_NAME, "low-price-remind"
                            )[0]
                        )
                    )
                    ele.click()

                while "(" not in self.driver.find_elements(
                    By.CLASS_NAME, "form-input-v3"
                )[1].get_attribute("value"):
                    # Enter搜索
                    # ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(its[1]))
                    # ele.send_keys(Keys.ENTER)
                    ele = WebDriverWait(self.driver, max_wait_time).until(
                        element_to_be_clickable(
                            self.driver.find_elements(
                                By.CLASS_NAME, "form-input-v3")[1]
                        )
                    )
                    ele.click()

                    # 通过低价提醒按钮实现enter键换页
                    ele = WebDriverWait(self.driver, max_wait_time).until(
                        element_to_be_clickable(
                            self.driver.find_elements(
                                By.CLASS_NAME, "low-price-remind"
                            )[0]
                        )
                    )
                    ele.click()

                next_stage_flag = True

        except Exception as e:
            # 错误次数+1
            self.err += 1

            # 保存错误截图
            if enable_screenshot:
                self.driver.save_screenshot(
                    f'screenshot/screenshot_{time.strftime("%Y-%m-%d_%H-%M-%S")}.png'
                )

            print(
                f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 错误次数【{self.err}-{max_retry_time}】,change_city：更换城市和日期失败，错误类型：{type(e).__name__}, 详细错误信息：{str(e).split("Stacktrace:")[0]}'
            )

            # 检查注意事项和验证码
            if self.check_verification_code():
                if self.err < max_retry_time:
                    if len(self.driver.find_elements(By.CLASS_NAME, "lg_loginbox_modal")):
                        print(
                            f'{time.strftime("%Y-%m-%d_%H-%M-%S")} change_city：检测到登录弹窗，需要登录'
                        )
                        self.login()
                    # 重试
                    print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} change_city：重试')
                    self.change_city()
                # 判断错误次数
                if self.err >= max_retry_time:
                    print(
                        f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 错误次数【{self.err}-{max_retry_time}】,change_city:重新尝试加载页面，这次指定需要重定向到首页'
                    )

                    # 删除本次请求
                    del self.driver.requests

                    # 重置错误计数
                    self.err = 0

                    # 重新尝试加载页面，这次指定需要重定向到首页
                    self.get_page(1)
        else:
            if next_stage_flag:
                # 若无错误，执行下一步
                self.get_data()

                print(
                    f'{time.strftime("%Y-%m-%d_%H-%M-%S")} change_city：成功更换城市和日期，当前路线为：{self.city[0]}-{self.city[1]}')

    def get_data(self):
        try:
            # 等待响应加载完成
            self.predata = self.driver.wait_for_request(
                "/international/search/api/search/batchSearch?.*", timeout=max_wait_time
            )
            
            if comft_flight:
                # 捕获 getFlightComfort 数据
                self.comfort_data = self.capture_flight_comfort_data()
            
            rb = dict(json.loads(self.predata.body).get("flightSegments")[0])

        except Exception as e:
            # 错误次数+1
            self.err += 1

            print(
                f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 错误次数【{self.err}-{max_retry_time}】,get_data:获取数据超时，错误类型：{type(e).__name__}, 错误详细：{str(e).split("Stacktrace:")[0]}'
            )

            # 保存错误截图
            if enable_screenshot:
                self.driver.save_screenshot(
                    f'screenshot/screenshot_{time.strftime("%Y-%m-%d_%H-%M-%S")}.png'
                )

            # 删除本次请求
            del self.driver.requests

            if self.err < max_retry_time:
                # 刷新页面
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} get_data：刷新页面')
                self.refresh_driver()

                # 检查注意事项和验证码
                if self.check_verification_code():
                    # 重试
                    self.get_data()

            # 判断错误次数
            if self.err >= max_retry_time:
                print(
                    f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 错误次数【{self.err}-{max_retry_time}】,get_data:重新尝试加载页面，这次指定需要重定向到首页'
                )

                # 重置错误计数
                self.err = 0
                # 重新尝试加载页面，这次指定需要重定向到首页
                self.get_page(1)
        else:
            # 删除本次请求
            del self.driver.requests

            # 检查数据获取正确性
            if (
                rb["departureCityName"] == self.city[0]
                and rb["arrivalCityName"] == self.city[1]
                and rb["departureDate"] == self.date
            ):
                print(f"get_data:城市匹配成功：出发地-{self.city[0]}，目的地-{self.city[1]}")

                # 重置错误计数
                self.err = 0

                # 若无错误，执行下一步
                self.decode_data()
            else:
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 错误次数【{self.err}-{max_retry_time}】,get_data:刷新页面')
                # 错误次数+1
                self.err += 1

                # 保存错误截图
                if enable_screenshot:
                    self.driver.save_screenshot(
                        f'screenshot/screenshot_{time.strftime("%Y-%m-%d_%H-%M-%S")}.png'
                    )

                # 重新更换城市
                print(
                    f'{time.strftime("%Y-%m-%d_%H-%M-%S")} get_data：重新更换城市:{rb["departureCityName"]}-{rb["arrivalCityName"]}-{rb["departureDate"]}'
                )

                # 检查注意事项和验证码
                if self.check_verification_code():
                    # 重试
                    self.change_city()

    def decode_data(self):
        try:
            # 使用python-magic库检查MIME类型
            mime = magic.Magic()
            file_type = mime.from_buffer(self.predata.response.body)

            buf = io.BytesIO(self.predata.response.body)

            if "gzip" in file_type:
                gf = gzip.GzipFile(fileobj=buf)
                self.dedata = gf.read().decode("UTF-8")
            elif "JSON data" in file_type:
                print(buf.read().decode("UTF-8"))
            else:
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 未知的压缩格式：{file_type}')
            
            self.dedata = json.loads(self.dedata)

        except Exception as e:
            # 错误次数+1
            self.err += 1

            print(
                f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 错误次数【{self.err}-{max_retry_time}】,decode_data:数据解码失败，错误类型：{type(e).__name__}, 错误详细：{str(e).split("Stacktrace:")[0]}'
            )

            # 保存错误截图
            if enable_screenshot:
                self.driver.save_screenshot(
                    f'screenshot/screenshot_{time.strftime("%Y-%m-%d_%H-%M-%S")}.png'
                )

            # 删除本次请求
            del self.driver.requests

            if self.err < max_retry_time:
                # 刷新页面
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} decode_data：刷新页面')
                self.refresh_driver()

                # 检查注意事项和验证码
                if self.check_verification_code():
                    # 重试
                    self.get_data()
            # 判错错误次数
            if self.err >= max_retry_time:
                print(
                    f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 错误次数【{self.err}-{max_retry_time}】,decode_data:重新尝试加载页面，这次指定需要重定向到首页'
                )

                # 重置错误计数
                self.err = 0

                # 重新尝试加载页面，这次指定需要重定向到首页
                self.get_page(1)
        else:
            # 重置错误计数
            self.err = 0

            # 若无错误，执行下一步
            self.check_data()

    def check_data(self):
        try:
            self.flightItineraryList = self.dedata["data"]["flightItineraryList"]
            # 倒序遍历,删除转机航班
            for i in range(len(self.flightItineraryList) - 1, -1, -1):
                if (
                    self.flightItineraryList[i]["flightSegments"][0]["transferCount"]
                    != 0
                ):
                    self.flightItineraryList.pop(i)
            if len(self.flightItineraryList) == 0 and direct_flight:
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 不存在直航航班:{self.city[0]}-{self.city[1]}')
                # 重置错误计数
                self.err = 0
                return 0
        except Exception as e:
            # 错误次数+1
            self.err += 1
            print(
                f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 数据检查出错：不存在航班，错误类型：{type(e).__name__}, 错误详细：{str(e).split("Stacktrace:")[0]}'
            )
            print(self.dedata)
            if self.err < max_retry_time:
                if 'searchErrorInfo' in self.dedata["data"]:
                    # 重置错误计数
                    self.err = 0
                    return 0
                else:
                    if "'needUserLogin': True" in str(self.dedata["data"]):
                        print(
                            f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 错误次数【{self.err}-{max_retry_time}】,check_data:必须要登录才能查看数据，这次指定需要重定向到首页'
                        )
                        # 重新尝试加载页面，这次指定需要重定向到首页
                        self.login()
                    
                    # 刷新页面
                    print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} check_data：刷新页面')
                    self.refresh_driver()
                    # 检查注意事项和验证码
                    if self.check_verification_code():
                        # 重试
                        self.get_data()
            # 判断错误次数
            if self.err >= max_retry_time:
                print(
                    f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 错误次数【{self.err}-{max_retry_time}】,check_data:重新尝试加载页面，这次指定需要重定向到首页'
                )

                # 重置错误计数
                self.err = 0

                # 重新尝试加载页面，这次指定需要重定向到首页
                self.get_page(1)
        else:
            # 重置错误计数
            self.err = 0
            self.proc_flightSegments()
            self.proc_priceList()
            self.mergedata()

    def proc_flightSegments(self):
        self.flights = pd.DataFrame()

        for flightlist in self.flightItineraryList:
            flightlist = flightlist["flightSegments"][0]["flightList"]
            flightUnitList = dict(flightlist[0])

            departureday = flightUnitList["departureDateTime"].split(" ")[0]
            departuretime = flightUnitList["departureDateTime"].split(" ")[1]

            arrivalday = flightUnitList["arrivalDateTime"].split(" ")[0]
            arrivaltime = flightUnitList["arrivalDateTime"].split(" ")[1]

            # 处理 stopList
            if 'stopList' in flightUnitList and flightUnitList['stopList']:
                stop_info = []
                for stop in flightUnitList['stopList']:
                    stop_info.append(f"{stop['cityName']}({stop['airportName']}, {stop['duration']}分钟)")
                flightUnitList['stopInfo'] = ' -> '.join(stop_info)
            else:
                flightUnitList['stopInfo'] = '无中转'

            if del_info:
                # 删除一些不重要的信息
                dellist = [
                    "sequenceNo",
                    "marketAirlineCode",
                    "departureProvinceId",
                    "departureCityId",
                    "departureCityCode",
                    "departureAirportShortName",
                    "departureTerminal",
                    "arrivalProvinceId",
                    "arrivalCityId",
                    "arrivalCityCode",
                    "arrivalAirportShortName",
                    "arrivalTerminal",
                    "transferDuration",
                    "stopList",
                    "leakedVisaTagSwitch",
                    "trafficType",
                    "highLightPlaneNo",
                    "mealType",
                    "operateAirlineCode",
                    "arrivalDateTime",
                    "departureDateTime",
                    "operateFlightNo",
                    "operateAirlineName",
                ]
                for value in dellist:
                    flightUnitList.pop(value, None)

            # 更新日期格式
            flightUnitList.update(
                {
                    "departureday": departureday,
                    "departuretime": departuretime,
                    "arrivalday": arrivalday,
                    "arrivaltime": arrivaltime,
                }
            )

            self.flights = pd.concat(
                [
                    self.flights,
                    pd.DataFrame.from_dict(flightUnitList, orient="index").T,
                ],
                ignore_index=True,
            )

    def proc_priceList(self):
        self.prices = pd.DataFrame()

        for flightlist in self.flightItineraryList:
            flightNo = flightlist["itineraryId"].split("_")[0]
            priceList = flightlist["priceList"]

            # 经济舱，经济舱折扣
            economy, economy_tax, economy_total, economy_full = [], [], [], []
            economy_origin_price, economy_tax_price, economy_total_price, economy_full_price = "", "", "", ""
            # 商务舱，商务舱折扣
            bussiness, bussiness_tax, bussiness_total, bussiness_full = [], [], [], []
            bussiness_origin_price, bussiness_tax_price, bussiness_total_price, bussiness_full_price = "", "", "", ""

            for price in priceList:
                # print("Price dictionary keys:", price.keys())
                # print("Full price dictionary:", json.dumps(price, indent=2))
                
                adultPrice = price["adultPrice"]
                childPrice = price.get("childPrice", adultPrice)  # 如果没有childPrice，使用adultPrice
                freeOilFeeAndTax = price["freeOilFeeAndTax"]
                sortPrice = price.get("sortPrice", adultPrice)  # 如果没有sortPrice，使用adultPrice
                
                # 估算税费（如果需要的话）
                estimatedTax = sortPrice - adultPrice if not freeOilFeeAndTax else 0
                adultTax = price.get("adultTax", estimatedTax)  # 如果没有adultTax，使用estimatedTax

                miseryIndex = price["miseryIndex"]
                cabin = price["cabin"]

                # 经济舱
                if cabin == "Y":
                    economy.append(adultPrice)
                    economy_tax.append(adultTax)
                    economy_full.append(miseryIndex)
                    economy_total.append(adultPrice+adultTax)
                # 商务舱
                elif cabin == "C":
                    bussiness.append(adultPrice)
                    bussiness_tax.append(adultTax)
                    bussiness_full.append(miseryIndex)
                    bussiness_total.append(adultPrice+adultTax)

            # 初始化变量
            economy_min_index = None
            bussiness_min_index = None
            
            if economy_total != []:
                economy_total_price = min(economy_total)
                economy_min_index = economy_total.index(economy_total_price)
            
            if bussiness_total != []:
                bussiness_total_price = min(bussiness_total)
                bussiness_min_index = bussiness_total.index(bussiness_total_price)
            
            if economy_min_index is not None:
                economy_origin_price = economy[economy_min_index]
                economy_tax_price = economy_tax[economy_min_index]
                economy_full_price = economy_full[economy_min_index]
            
            if bussiness_min_index is not None:
                bussiness_origin_price = bussiness[bussiness_min_index]
                bussiness_tax_price = bussiness_tax[bussiness_min_index]
                bussiness_full_price = bussiness_full[bussiness_min_index]
            
            price_info = {
                "flightNo": flightNo,
                "economy_origin": economy_origin_price,
                "economy_tax": economy_tax_price,
                "economy_total": economy_total_price,
                "economy_full": economy_full_price,
                "bussiness_origin": bussiness_origin_price,
                "bussiness_tax": bussiness_tax_price,
                "bussiness_total": bussiness_total_price,
                "bussiness_full": bussiness_full_price,
            }

            # self.prices=self.prices.append(price_info,ignore_index=True)
            self.prices = pd.concat(
                [self.prices, pd.DataFrame(price_info, index=[0])], ignore_index=True
            )

    def mergedata(self):
        try:
            self.df = self.flights.merge(self.prices, on=["flightNo"])
            print(f"合并后的航班数据形状: {self.df.shape}")
            print(f"合并后的航班数据列: {self.df.columns}")

            self.df["dateGetTime"] = dt.now().strftime("%Y-%m-%d")

            print(f"获取到的舒适度数据: {self.comfort_data}")
            
            # 数据的列名映射
            columns = {
                "dateGetTime": "数据获取日期",
                "flightNo": "航班号",
                "marketAirlineName": "航空公司",
                "departureday": "出发日期",
                "departuretime": "出发时间",
                "arrivalday": "到达日期",
                "arrivaltime": "到达时间",
                "duration": "飞行时长",
                "departureCountryName": "出发国家",
                "departureCityName": "出发城市",
                "departureAirportName": "出发机场",
                "departureAirportCode": "出发机场三字码",
                "arrivalCountryName": "到达国家",
                "arrivalCityName": "到达城市",
                "arrivalAirportName": "到达机场",
                "arrivalAirportCode": "到达机场三字码",
                "aircraftName": "飞机型号",
                "aircraftSize": "飞机尺寸",
                "aircraftCode": "飞机型号三字码",
                "arrivalPunctuality": "到达准点率",
                "stopCount": "停留次数",
                "stopInfo": "中转信息"
            }
            
            # 定义舒适度数据的列名映射
            comfort_columns = {
                'departure_delay_time': '出发延误时间',
                'departure_bridge_rate': '出发廊桥率',
                'arrival_delay_time': '到达延误时间',
                'plane_type': '飞机类型',
                'plane_width': '飞机宽度',
                'plane_age': '飞机机龄',
                'Y_has_meal': '经济舱是否有餐食',
                'Y_seat_tilt': '经济舱座椅倾斜度',
                'Y_seat_width': '经济舱座椅宽度',
                'Y_seat_pitch': '经济舱座椅间距',
                'Y_meal_msg': '经济舱餐食信息',
                'Y_power': '经济舱电源',
                'C_has_meal': '商务舱是否有餐食',
                'C_seat_tilt': '商务舱座椅倾斜度',
                'C_seat_width': '商务舱座椅宽度',
                'C_seat_pitch': '商务舱座椅间距',
                'C_meal_msg': '商务舱餐食信息',
                'C_power': '商务舱电源',
            }
            
            if self.comfort_data:
                comfort_df = pd.DataFrame.from_dict(self.comfort_data, orient='index')
                comfort_df.reset_index(inplace=True)
                comfort_df.rename(columns={'index': 'flight_no'}, inplace=True)
                
                print(f"舒适度数据形状: {comfort_df.shape}")
                print(f"舒适度数据列: {comfort_df.columns}")
                print(f"舒适度数据前几行: \n{comfort_df.head()}")
                
                # 检查 operateFlightNo 列是否存在
                if 'operateFlightNo' in self.df.columns:
                    print(f"合并前的 operateFlightNo 唯一值: {self.df['operateFlightNo'].unique()}")
                    # 创建一个临时列来存储用于匹配的航班号
                    self.df['match_flight_no'] = self.df['operateFlightNo'].fillna(self.df['flightNo'])
                else:
                    print("警告: operateFlightNo 列不存在于数据中,将使用 flightNo 进行匹配")
                    self.df['match_flight_no'] = self.df['flightNo']
                
                print(f"现有的列: {self.df.columns}")
                print(f"合并前的 flight_no 唯一值: {comfort_df['flight_no'].unique()}")
                
                # 使用 left join 来合并数据
                self.df = self.df.merge(comfort_df, left_on='match_flight_no', right_on='flight_no', how='left')
                
                print(f"合并后的数据形状: {self.df.shape}")
                print(f"合并后的数据列: {self.df.columns}")
                
                # 删除临时列和多余的flight_no列
                self.df.drop(['match_flight_no', 'flight_no'], axis=1, inplace=True, errors='ignore')
            else:
                # 如果没有舒适度数据，手动添加空列，保证数据结构一致性
                for col in comfort_columns.keys():
                    self.df[col] = None  # 添加缺失的舒适度列并填充为空值

            if rename_col:
                order = list(columns.values())
                # 对pandas的columns进行重命名
                columns.update(comfort_columns, errors='ignore')

                self.df = self.df.rename(columns=columns)

                if del_info:
                    # 使用 reindex 确保所有列都存在于最终的 DataFrame 中，不存在的列会被自动忽略
                    self.df = self.df.reindex(columns=order, fill_value=None)

            files_dir = os.path.join(
                os.getcwd(), self.date, dt.now().strftime("%Y-%m-%d")
            )

            if not os.path.exists(files_dir):
                os.makedirs(files_dir)

            filename = os.path.join(
                files_dir, f"{self.city[0]}-{self.city[1]}.csv")

            self.df.to_csv(filename, encoding="UTF-8", index=False)

            print(f'\n{time.strftime("%Y-%m-%d_%H-%M-%S")} 数据爬取完成 {filename}\n')

            return 0

        except Exception as e:
            print(f"合并数据失败 {str(e)}")
            print(f"错误类型: {type(e).__name__}")
            print(f"错误详情: {str(e)}")
            import traceback
            print(f"错误堆栈: {traceback.format_exc()}")
            return 0

    def capture_flight_comfort_data(self):
        try:
            # 滚动页面到底部以加载所有内容
            last_height = self.driver.execute_script("return document.body.scrollHeight")
            while True:
                # 分步滚动页面
                for i in range(10):  # 将页面分成10步滚动
                    scroll_height = last_height * (i + 1) / 3
                    self.driver.execute_script(f"window.scrollTo(0, {scroll_height});")
                    time.sleep(0.5)  # 每一小步等待0.5秒
                
                # 等待页面加载
                time.sleep(3)  # 滚动到底部后多等待3秒
                
                # 计算新的滚动高度并与最后的滚动高度进行比较
                new_height = self.driver.execute_script("return document.body.scrollHeight")
                if new_height == last_height:
                    break
                last_height = new_height

            comfort_requests = self.driver.requests
            comfort_data = {}
            batch_comfort_found = False
            getFlightComfort_requests_count = 0
            total_requests_count = len(comfort_requests)

            print(f"\n{time.strftime('%Y-%m-%d_%H-%M-%S')} 开始分析请求，总请求数：{total_requests_count}")

            for request in comfort_requests:
                if "/search/api/flight/comfort/batchGetComfortTagList" in request.url:
                    batch_comfort_found = True
                    print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} 找到 batchGetComfortTagList 请求")
                    continue
                
                if "/search/api/flight/comfort/getFlightComfort" in request.url:
                    getFlightComfort_requests_count += 1
                    print(f"\n{time.strftime('%Y-%m-%d_%H-%M-%S')} 捕获到第 {getFlightComfort_requests_count} 个 getFlightComfort 请求:")
                    print(f"URL: {request.url}")
                    
                    try:
                        payload = json.loads(request.body.decode('utf-8'))
                        flight_no = payload.get('flightNoList', ['Unknown'])[0]
                        print(f"请求的航班号: {flight_no}")
                    except Exception as e:
                        print(f"无法解析请求 payload: {str(e)}")
                        continue

                    if request.response:
                        print(f"响应状态码: {request.response.status_code}")
                        body = request.response.body
                        if request.response.headers.get('Content-Encoding', '').lower() == 'gzip':
                            body = gzip.decompress(body)
                        
                        try:
                            json_data = json.loads(body.decode('utf-8'))
                            print(f"响应数据: {json.dumps(json_data, indent=2, ensure_ascii=False)[:500]}...")  # 打印前500个字符
                            if json_data['status'] == 0 and json_data['msg'] == 'success':
                                flight_comfort = json_data['data']
                                
                                punctuality = flight_comfort['punctualityInfo']
                                plane_info = flight_comfort['planeInfo']
                                cabin_info = {cabin['cabin']: cabin for cabin in flight_comfort['cabinInfoList']}
                                
                                processed_data = {
                                    'departure_delay_time': punctuality.get("departureDelaytime", None),
                                    'departure_bridge_rate': punctuality.get("departureBridge", None),
                                    'arrival_delay_time': punctuality.get("arrivalDelaytime", None),
                                    'plane_type': plane_info.get("planeTypeName", None),
                                    'plane_width': plane_info.get("planeWidthCategory", None),
                                    'plane_age': plane_info.get("planeAge", None)
                                }
                                
                                for cabin_type in ['Y', 'C']:
                                    if cabin_type in cabin_info:
                                        cabin = cabin_info[cabin_type]
                                        processed_data.update({
                                            f'{cabin_type}_has_meal': cabin['hasMeal'],
                                            f'{cabin_type}_seat_tilt': cabin['seatTilt']['value'],
                                            f'{cabin_type}_seat_width': cabin['seatWidth']['value'],
                                            f'{cabin_type}_seat_pitch': cabin['seatPitch']['value'],
                                            f'{cabin_type}_meal_msg': cabin['mealMsg']
                                        })
                                        if 'power' in cabin:
                                            processed_data[f'{cabin_type}_power'] = cabin['power']
                                
                                comfort_data[flight_no] = processed_data
                                print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} 成功提取航班 {flight_no} 的舒适度数据")
                            else:
                                print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} getFlightComfort 响应状态异常: {json_data['status']}, {json_data['msg']}")
                        except Exception as e:
                            print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} 处理 getFlightComfort 响应时出错: {str(e)}")
                    else:
                        print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} getFlightComfort 请求没有响应")

            print(f"\n{time.strftime('%Y-%m-%d_%H-%M-%S')} 请求分析完成")
            print(f"总请求数: {total_requests_count}")
            print(f"batchGetComfortTagList 请求是否找到: {batch_comfort_found}")
            print(f"getFlightComfort 请求数: {getFlightComfort_requests_count}")
            print(f"成功提取的舒适度数据数: {len(comfort_data)}")

            if comfort_data:
                # 创建舒适度DataFrame
                comfort_df = pd.DataFrame.from_dict(comfort_data, orient='index')
                comfort_df.reset_index(inplace=True)
                comfort_df.rename(columns={'index': 'flight_no'}, inplace=True)
                
                # 保存舒适度数据为CSV文件
                # save_dir = os.path.join(os.getcwd(), self.date, datetime.now().strftime("%Y-%m-%d"))
                # os.makedirs(save_dir, exist_ok=True)
                
                # comfort_filename = os.path.join(save_dir, f"{self.city[0]}-{self.city[1]}_comfort.csv")
                # comfort_df.to_csv(comfort_filename, encoding="UTF-8", index=False)
                # print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} 航班舒适度数据已保存到 {comfort_filename}")
                
                return comfort_data
            else:
                print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} 未捕获到任何 getFlightComfort 数据")
                print("可能的原因:")
                print("1. 网页没有加载完全")
                print("2. 网站结构可能已经改变")
                print("3. 网络连接问题")
                print("4. 请求被网站拦截或限制")
                return None

        except Exception as e:
            print(f"{time.strftime('%Y-%m-%d_%H-%M-%S')} 捕获 getFlightComfort 数据时出错：{str(e)}")
            print(f"错误类型: {type(e).__name__}")
            print(f"错误详情: {str(e)}")
            import traceback
            print(f"错误堆栈: {traceback.format_exc()}")
            return None


if __name__ == "__main__":

    driver = init_driver()

    citys = gen_citys(crawl_citys)

    flight_dates = generate_flight_dates(crawl_days, begin_date, end_date, start_interval, days_interval)

    Flight_DataFetcher = DataFetcher(driver)

    for city in citys:
        Flight_DataFetcher.city = city

        for flight_date in flight_dates:
            Flight_DataFetcher.date = flight_date

            if os.path.exists(os.path.join(os.getcwd(), flight_date, dt.now().strftime("%Y-%m-%d"), f"{city[0]}-{city[1]}.csv")):
                print(
                    f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 文件已存在:{os.path.join(os.getcwd(), flight_date, dt.now().strftime("%Y-%m-%d"), f"{city[0]}-{city[1]}.csv")}')
                continue
            elif ('http' not in Flight_DataFetcher.driver.current_url):
                print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} 当前的URL是：{driver.current_url}')
                # 初始化页面
                Flight_DataFetcher.get_page(1)

            else:
                # 后续运行只需更换出发与目的地
                Flight_DataFetcher.change_city()

            time.sleep(crawl_interval)

    # 运行结束退出
    try:
        driver = Flight_DataFetcher.driver
        driver.quit()
    except Exception as e:
        print(f'{time.strftime("%Y-%m-%d_%H-%M-%S")} An error occurred while quitting the driver: {e}')

    print(f'\n{time.strftime("%Y-%m-%d_%H-%M-%S")} 程序运行完成！！！！')
