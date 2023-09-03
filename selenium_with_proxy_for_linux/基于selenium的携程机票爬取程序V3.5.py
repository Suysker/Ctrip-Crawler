import gen_proxy_servers
import magic
import io
import os
import gzip
import time
import json
import pandas as pd
from seleniumwire import webdriver
from datetime import datetime as dt,timedelta
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException,StaleElementReferenceException,ElementNotInteractableException,ElementClickInterceptedException # 加载异常
from selenium.webdriver.support.ui import WebDriverWait


#爬取的城市
crawal_citys = ['上海', '广州', '深圳', '北京']

#爬取的日期
crawal_days =60

# 设置各城市爬取的时间间隔（单位：秒）
crawal_interval = 5

#日期间隔
days_interval = 5

# 设置页面加载的最长等待时间（单位：秒）
max_wait_time = 10

# 是否只抓取直飞信息（True: 只抓取直飞，False: 抓取所有航班）
direct_flight = True

# 是否删除不重要的信息
del_info = False

# 是否重命名DataFrame的列名
rename_col = True

#开启代理
enable_proxy=True

#生成代理IPV6数量
ipv6_count=100

#起始端口
start_port=20000

#服务端口
proxy_port=10000

#服务器地址
proxy_address='127.0.0.1'

#生成的IPV6接口名称
base_interface='eth0'

def kill_driver():
    os.system('''ps -ef | grep chrome | grep -v grep | awk '{print "kill -9" $2}'| sh''')

def init_driver():
    options = webdriver.ChromeOptions()  # 创建一个配置对象
    if enable_proxy:
        options.add_argument(f"--proxy-server=http://{proxy_address}:{proxy_port}")  # 指定代理服务器和端口
    options.add_argument('--incognito')  # 隐身模式（无痕模式）
    options.add_argument('--headless')  # 启用无头模式
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument("--disable-blink-features")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ['enable-automation'])  # 不显示正在受自动化软件控制的提示
    #options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36 Edg/116.0.1938.69")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(300)  # 设置加载超时阈值
    driver.maximize_window()

    return driver

def gen_citys(crawal_citys):
    # 生成城市组合列表
    citys = []
    ytic = list(reversed(crawal_citys))
    for m in crawal_citys:
        for n in ytic:
            if m == n:
                continue
            else:
                citys.append([m, n])
    return citys

def generate_flight_dates(n, days_interval):
    
    flight_dates = []
    
    for i in range(0, n, days_interval):
        
        flight_date = dt.now() + timedelta(days=i + 1)
        
        flight_dates.append(flight_date.strftime('%Y-%m-%d'))
    
    return flight_dates

#element_to_be_clickable 函数来替代 expected_conditions.element_to_be_clickable 或 expected_conditions.visibility_of_element_located
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
    def __init__(self,driver):
        self.driver=driver
        self.date=None
        self.city=None
        self.err=0#错误重试次数
        self.flights = pd.DataFrame()
        self.prices = pd.DataFrame()
        

    def remove_btn(self):
        try:
            WebDriverWait(self.driver, max_wait_time).until(lambda d: d.execute_script('return typeof jQuery !== "undefined"'))
            js_remove="$('.notice-box').remove();"
            self.driver.execute_script(js_remove)
        except Exception as e:
            print('提醒移除失败',str(e).split("Stacktrace:")[0])

    def get_page(self,reset_to_homepage=0): 
        try:
            if reset_to_homepage == 1:
                #前往首页
                self.driver.get('https://flights.ctrip.com/online/channel/domestic')
            
            #移除提醒
            self.remove_btn()
            
            WebDriverWait(self.driver, max_wait_time).until(EC.presence_of_element_located((By.CLASS_NAME, 'pc_home-jipiao')))
            #点击飞机图标，返回主界面
            ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_element(By.CLASS_NAME,'pc_home-jipiao')))
            ele.click()
            
            #单程
            ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_elements(By.CLASS_NAME,'radio-label')[0]))
            ele.click()
                               
            #搜索
            ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_element(By.CLASS_NAME,'search-btn')))
            ele.click()
            
        except Exception as e:
            # 用f字符串格式化错误类型和错误信息，提供更多的调试信息
            print(f'页面加载或元素操作失败，错误类型：{type(e).__name__}, 详细错误信息：{str(e).split("Stacktrace:")[0]}')
            # 重新尝试加载页面，这次指定需要重定向到首页
            #self.driver.close()
            self.get_page(1)
        else:
            try:
                # 检查是否有验证码元素，如果有，则需要人工处理
                self.driver.find_element(By.ID, "verification-code")
                print('验证码被触发，等待2小时后或人工处理再进行重试。')
                self.driver.quit()
                time.sleep(7200)
                self.driver=init_driver()
                #更换IPV6地址
                if enable_proxy:
                    gen_proxy_servers.switch_proxy_server()
                self.get_page(1)
            except:
                # 如果没有找到验证码元素，则说明页面加载成功，没有触发验证码
                print('页面成功加载，未触发验证码，即将进行下一步操作。')
                if self.city:
                    self.change_city()

    def change_city(self):
        
        #移除提醒提醒
        self.remove_btn()
        
        try:
            #获取出发地与目的地元素位置
            #its=self.driver.find_elements(By.CLASS_NAME,'form-input-v3')
            
            WebDriverWait(self.driver, max_wait_time).until(EC.presence_of_element_located((By.CLASS_NAME, 'form-input-v3')))
            #若出发地与目标值不符，则更改出发地
            while self.city[0] not in self.driver.find_elements(By.CLASS_NAME,'form-input-v3')[0].get_attribute('value'):    
                ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_elements(By.CLASS_NAME,'form-input-v3')[0]))
                ele.click()
                ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_elements(By.CLASS_NAME,'form-input-v3')[0]))
                ele.send_keys(Keys.CONTROL + 'a')
                ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_elements(By.CLASS_NAME,'form-input-v3')[0]))
                ele.send_keys(self.city[0])
                
            #若目的地与目标值不符，则更改目的地
            while self.city[1] not in self.driver.find_elements(By.CLASS_NAME,'form-input-v3')[1].get_attribute('value'):
                ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_elements(By.CLASS_NAME,'form-input-v3')[1]))
                ele.click()
                ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_elements(By.CLASS_NAME,'form-input-v3')[1]))
                ele.send_keys(Keys.CONTROL + 'a')
                ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_elements(By.CLASS_NAME,'form-input-v3')[1]))
                ele.send_keys(self.city[1])
            
            while self.driver.find_elements(By.CSS_SELECTOR,"[aria-label=请选择日期]")[0].get_attribute("value") != self.date:
                #点击日期选择
                ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_element(By.CLASS_NAME,'modifyDate.depart-date')))
                ele.click()
                
                for m in self.driver.find_elements(By.CLASS_NAME,'date-picker.date-picker-block'):
                    
                    if int(m.find_element(By.CLASS_NAME,'month').text[:-1]) != int(self.date[5:7]):
                        continue
                    
                    for d in m.find_elements(By.CLASS_NAME,'date-d'):
                        if int(d.text) == int(self.date[-2:]):
                            ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(d))
                            ele.click()
                            break 
            
            # 验证目的地名称是否完整
            try:
                while '(' not in self.driver.find_elements(By.CLASS_NAME,'form-input-v3')[1].get_attribute('value'):
                    #Enter搜索
                    #ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(its[1]))
                    #ele.send_keys(Keys.ENTER)
                    
                    #移除提醒提醒
                    self.remove_btn()
                    
                    #通过低价提醒按钮实现enter键换页
                    ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_elements(By.CLASS_NAME,'low-price-remind')[0]))
                    ele.click()
            except IndexError as e:
                print(f'更换城市失败，错误类型：{type(e).__name__}, 错误信息：{str(e).split("Stacktrace:")[0]}')
                #以防万一
                ele=WebDriverWait(self.driver, max_wait_time).until(element_to_be_clickable(self.driver.find_elements(By.CLASS_NAME,'form-input-v3')[1]))
                ele.send_keys(Keys.ENTER)
            
            print(f'成功更换城市，当前路线为：{self.city[0]}-{self.city[1]}')
        #捕获错误
        except (IndexError,ElementNotInteractableException,StaleElementReferenceException,ElementClickInterceptedException,ElementClickInterceptedException) as e:

            try:
                # 检查是否有验证码元素，如果有，则需要人工处理
                self.driver.find_element(By.CLASS_NAME, "alert-title")
                print('验证码被触发，等待2小时后或人工处理再进行重试。')
                self.driver.quit()
                time.sleep(7200)
                self.driver=init_driver()
                #更换IPV6地址
                if enable_proxy:
                    gen_proxy_servers.switch_proxy_server()
                self.get_page(1)
            except:
                print('')
            
            print(f'更换城市失败，错误类型：{type(e).__name__}, 错误信息：{str(e).split("Stacktrace:")[0]}')
            self.err+=1
            if self.err<=5:
                self.change_city()
            else:
                self.err=0
                del self.driver.requests
                self.get_page()
        except Exception as e:
            print(f'更换城市失败，错误类型：{type(e).__name__}, 错误信息：{str(e).split("Stacktrace:")[0]}')         
            #删除本次请求
            del self.driver.requests
            #从头开始重新执行程序
            self.get_page()
        else:
            #若无错误，执行下一步
            self.err=0
            self.get_data()                 
    
    def get_data(self):
        try:
            #等待响应加载完成
            self.predata = self.driver.wait_for_request('/international/search/api/search/batchSearch?.*', timeout=30)
        
            rb=dict(json.loads(self.predata.body).get('flightSegments')[0])
        
        except TimeoutException as e:
            print(f'获取数据超时，错误类型：{type(e).__name__}, 错误详细：{str(e).split("Stacktrace:")[0]}')
            #删除本次请求
            del self.driver.requests
            #从头开始重新执行程序
            self.get_page()
        else:
            #检查数据获取正确性
            if rb['departureCityName'] == self.city[0] and rb['arrivalCityName'] == self.city[1]:
                print(f'城市匹配成功：出发地-{self.city[0]}，目的地-{self.city[1]}')
                #删除本次请求
                del self.driver.requests
                #若无错误，执行下一步
                self.decode_data()
            else:
                #删除本次请求
                del self.driver.requests
                #重新更换城市
                self.change_city()
    
    def decode_data(self):
        try:
            # 使用python-magic库检查MIME类型
            mime = magic.Magic()
            file_type = mime.from_buffer(self.predata.response.body)
            
            buf = io.BytesIO(self.predata.response.body)
            
            if "gzip" in file_type:
                gf = gzip.GzipFile(fileobj=buf)
                self.dedata = gf.read().decode('UTF-8')
            elif "JSON data" in file_type:
                print(buf.read().decode('UTF-8'))
            else:
                print(f'未知的压缩格式：{file_type}')
            
            self.dedata = json.loads(self.dedata)
        
        except Exception as e:
            print(f'数据解码失败，错误类型：{type(e).__name__}, 错误详细：{str(e).split("Stacktrace:")[0]}')

            try:
                # 检查是否有验证码元素，如果有，则需要人工处理
                self.driver.find_element(By.CLASS_NAME, "alert-title")
                print('验证码被触发，等待2小时后或人工处理再进行重试。')
                self.driver.quit()
                time.sleep(7200)
                self.driver=init_driver()
                #更换IPV6地址
                if enable_proxy:
                    gen_proxy_servers.switch_proxy_server()
                self.get_page(1)
            except:
                print('')
            
            self.get_page()
            
    def check_data(self):
        try:
            self.flightItineraryList=self.dedata['data']['flightItineraryList']
            #倒序遍历,删除转机航班
            for i in range(len(self.flightItineraryList)-1, -1, -1):
                if self.flightItineraryList[i]['flightSegments'][0]['transferCount'] !=0:
                    self.flightItineraryList.pop(i)
            if len(self.flightItineraryList)==0 and direct_flight:
                print(f'不存在直航航班:{self.city[0]}-{self.city[1]}')
                return 0
            else:
                self.proc_flightSegments()
                self.proc_priceList()
                self.mergedata()
        except Exception as e:
            print(f'数据检查出错：不存在航班，错误类型：{type(e).__name__}, 错误详细：{str(e).split("Stacktrace:")[0]}')
            return 0        
    
    def proc_flightSegments(self):
        for flightlist in self.flightItineraryList:
            flightlist=flightlist['flightSegments'][0]['flightList']
            flightUnitList=dict(flightlist[0])

            
            departureday=flightUnitList['departureDateTime'].split(' ')[0]
            departuretime=flightUnitList['departureDateTime'].split(' ')[1]
            
            arrivalday=flightUnitList['arrivalDateTime'].split(' ')[0]
            arrivaltime=flightUnitList['arrivalDateTime'].split(' ')[1]            
            
            if del_info:
                #删除一些不重要的信息
                dellist=['sequenceNo', 'marketAirlineCode',
                'departureProvinceId','departureCityId','departureCityCode','departureAirportShortName','departureTerminal',
                'arrivalProvinceId','arrivalCityId','arrivalCityCode','arrivalAirportShortName','arrivalTerminal',
                'transferDuration','stopList','leakedVisaTagSwitch','trafficType','highLightPlaneNo','mealType',
                'operateAirlineCode','arrivalDateTime','departureDateTime','operateFlightNo','operateAirlineName']
                for value in dellist:
                    try:
                        flightUnitList.pop(value)
                    except:
                        continue
                
            #更新日期格式
            flightUnitList.update({'departureday': departureday, 'departuretime': departuretime,
                                   'arrivalday': arrivalday, 'arrivaltime': arrivaltime}) 
            
            self.flights=pd.concat([self.flights,pd.DataFrame.from_dict(flightUnitList, orient='index').T],ignore_index=True)

                          
            
    def proc_priceList(self):
        for flightlist in self.flightItineraryList:
            flightNo=flightlist['itineraryId'].split('_')[0]
            priceList=flightlist['priceList']
            
            #经济舱，经济舱折扣
            economy,economy_discount=[],[]
            #商务舱，商务舱折扣
            bussiness,bussiness_discount=[],[]
            
            for price in priceList:
                adultPrice=price['adultPrice']
                cabin=price['cabin']
                priceUnitList=dict(price['priceUnitList'][0]['flightSeatList'][0])
                try:
                    discountRate=priceUnitList['discountRate']
                except:
                    discountRate=1
                #经济舱
                if cabin=='Y':
                    economy.append(adultPrice)
                    economy_discount.append(discountRate)
                 #商务舱
                elif cabin=='C':
                    bussiness.append(adultPrice)
                    bussiness_discount.append(discountRate)
            
            if economy !=[]:
                try:
                    economy_origin=economy[economy_discount.index(1)]
                except:
                    economy_origin=int(max(economy)/max(economy_discount))
            
                if min(economy_discount) !=1:
                    economy_low=min(economy)
                    economy_cut=min(economy_discount)
                else:
                    economy_low=''
                    economy_cut=''
                
            else:
                economy_origin=''
                economy_low=''
                economy_cut=''
            

            if bussiness !=[]: 
                try:
                    bussiness_origin=bussiness[bussiness_discount.index(1)]
                except:
                    bussiness_origin=int(max(bussiness)/max(bussiness_discount))
            
                if min(bussiness_discount) !=1:
                    bussiness_low=min(bussiness)
                    bussiness_cut=min(bussiness_discount)
                else:
                    bussiness_low=''
                    bussiness_cut=''
                
            else:
                bussiness_origin=''
                bussiness_low=''
                bussiness_cut=''        
        
            price_info={'flightNo':flightNo,
                    'economy_origin':economy_origin,'economy_low':economy_low,'economy_cut':economy_cut,
                    'bussiness_origin':bussiness_origin,'bussiness_low':bussiness_low,'bussiness_cut':bussiness_cut}

            #self.prices=self.prices.append(price_info,ignore_index=True)
            self.prices=pd.concat([self.prices,pd.DataFrame(price_info,index=[0])],ignore_index=True)
        
   
   
    def mergedata(self):
        try:
            self.df = self.flights.merge(self.prices,on=['flightNo'])
            
            self.df['dateGetTime']=dt.now().strftime('%Y-%m-%d')
            
            if rename_col:
                #对pandas的columns进行重命名
                order=['数据获取日期','航班号','航空公司',
                    '出发日期','出发时间','到达日期','到达时间','飞行时长','出发国家','出发城市','出发机场','出发机场三字码',
                    '到达国家','到达城市','到达机场','到达机场三字码','飞机型号','飞机尺寸','飞机型号三字码',
                    '经济舱原价','经济舱最低价','经济舱折扣','商务舱原价','商务舱最低价','商务舱折扣',
                    '到达准点率','停留次数']
                
                origin=['dateGetTime','flightNo','marketAirlineName',
                        'departureday','departuretime','arrivalday','arrivaltime','duration',
                        'departureCountryName','departureCityName','departureAirportName','departureAirportCode',
                        'arrivalCountryName','arrivalCityName','arrivalAirportName','arrivalAirportCode',
                        'aircraftName','aircraftSize','aircraftCode',
                        'economy_origin','economy_low','economy_cut',
                        'bussiness_origin','bussiness_low','bussiness_cut',
                        'arrivalPunctuality','stopCount']
            
                columns=dict(zip(origin,order))

                self.df=self.df.rename(columns=columns)
              
                if del_info:
                    self.df = self.df[order]
            
            
            files_dir = os.path.join(os.getcwd(), self.date, dt.now().strftime('%Y-%m-%d'))
            
            if not os.path.exists(files_dir):
                os.makedirs(files_dir)
            
            filename = os.path.join(files_dir, f"{self.city[0]}-{self.city[1]}.csv")

            self.df.to_csv(filename,encoding='GB18030',index=False)
            
            print('\n数据爬取完成',filename) 
        except Exception as e:
            print('合并数据失败',str(e).split("Stacktrace:")[0])


if __name__ == '__main__':
    
    kill_driver()
    
    if enable_proxy:
        gen_proxy_servers.start_proxy_servers(ipv6_count,start_port,proxy_port,base_interface)

    driver=init_driver()
    
    citys=gen_citys(crawal_citys)

    flight_dates=generate_flight_dates(crawal_days,days_interval)

    Flight_DataFetcher=DataFetcher(driver)

    for flight_date in flight_dates:
        
        Flight_DataFetcher.date=flight_date

        if flight_dates.index(flight_date)==0:
            #第一次运行
            Flight_DataFetcher.get_page(1)

        for city in citys:

            #后续运行只需更换出发与目的地
            Flight_DataFetcher.city=city
            Flight_DataFetcher.change_city()

            if Flight_DataFetcher.dedata:
                Flight_DataFetcher.check_data()
                
            #更换IPV6地址
            if enable_proxy:
                gen_proxy_servers.switch_proxy_server()
                
            time.sleep(crawal_interval)
        
    #运行结束退出
    try:
        driver=Flight_DataFetcher.driver
        driver.quit()
    except Exception as e:
        print(f"An error occurred while quitting the driver: {e}")
    
    if enable_proxy:
        gen_proxy_servers.stop_proxy_servers(ipv6_count,base_interface)

    print('\n程序运行完成！！！！')    