import io
import os
import gzip
import time
import json
import random
import requests
import threading
import pandas as pd
from seleniumwire import webdriver
from datetime import datetime as dt,timedelta
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException,StaleElementReferenceException,ElementNotInteractableException,ElementClickInterceptedException # 加载异常


def getcitycode():
    cityname,code=[],[]
    #采用携程的api接口
    city_url='https://flights.ctrip.com/online/api/poi/get?v='+str(random.random())
    headers={
        'dnt':'1',
        'referer':'https://verify.ctrip.com/',
        'user-agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36'
        }
    r=requests.get(city_url,headers=headers)
    citys=json.loads(r.text).get('data')
    for city in citys:
        if city =='热门':
            continue
        for key in city:
            try:
                for k in citys[city][key]:
                    cityname.append(k['display'])
                    code.append(k['data'])
            except:
                continue
    citycode=dict(zip(cityname,code))
    
    return cityname,citycode



class FLIGHT(object):
    def __init__(self):
        self.url = 'https://flights.ctrip.com/online/list/oneway' #携程机票查询页面
        self.chromeDriverPath = 'C:/Program Files/Google/Chrome/Application/chromedriver' #chromedriver位置
        self.options = webdriver.ChromeOptions() # 创建一个配置对象
        #self.options.add_argument('--incognito')  # 隐身模式（无痕模式）
        #self.options.add_argument('User-Agent=%s'%UserAgent().random) # 替换User-Agent
        self.options.add_argument("--disable-blink-features")
        self.options.add_argument("--disable-blink-features=AutomationControlled")
        self.options.add_experimental_option("excludeSwitches", ['enable-automation'])# 不显示正在受自动化软件控制
        self.driver = webdriver.Chrome(executable_path=self.chromeDriverPath,chrome_options=self.options)
        self.driver.maximize_window()
        self.err=0#错误重试次数
          
    
    def getpage(self): 
        ##############获取地区码
        self.startcode=self.citycode[self.city[0]][-3:]
        self.endcode=self.citycode[self.city[1]][-3:]
        
        ##############生成访问链接
        flights_url=self.url+'-'+self.startcode+'-'+self.endcode+'?&depdate='+self.date    
        print(flights_url)
        ##############设置加载超时阈值
        self.driver.set_page_load_timeout(300)
        try:
            self.driver.get(flights_url)
        except:
            print('页面连接失败')
            self.driver.close()
            self.getpage()
        else:
            try:
                ##############判断是否存在验证码
                self.driver.find_element(By.CLASS_NAME,"basic-alert.alert-giftinfo")
                print('等待2小时后重试')
                time.sleep(7200)
                self.getpage()
            except:
                ##############不存在验证码，执行下一步
                self.remove_btn()

    def remove_btn(self):
        try:
            js_remove="$('.notice-box').remove();"
            self.driver.execute_script(js_remove)
        except Exception as e:
            print('防疫移除失败',e)
        else:
            self.changecity()

    
    
    def changecity(self):
        try:
        	#获取出发地与目的地元素位置
            its=self.driver.find_elements(By.CLASS_NAME,'form-input-v3')
            
            #若出发地与目标值不符，则更改出发地
            while self.city[0] not in its[0].get_attribute('value'):    
                its[0].click()
                time.sleep(0.5)
                its[0].send_keys(Keys.CONTROL + 'a')
                time.sleep(0.5)
                its[0].send_keys(self.city[0])

            time.sleep(0.5)

            #若目的地与目标值不符，则更改目的地
            while self.city[1] not in its[1].get_attribute('value'):
                its[1].click()
                time.sleep(0.5)
                its[1].send_keys(Keys.CONTROL + 'a')
                time.sleep(0.5)
                its[1].send_keys(self.city[1])
            
            time.sleep(0.5)
            try:
                #通过低价提醒按钮实现enter键换页
                self.driver.implicitly_wait(5) # seconds
                self.driver.find_elements(By.CLASS_NAME,'low-price-remind')[0].click()
            except IndexError as e:
                print('\n更换城市错误 找不到元素',e)
                #以防万一
                its[1].send_keys(Keys.ENTER)
            
            print('\n更换城市成功',self.city[0]+'-'+self.city[1])
        except (ElementNotInteractableException,StaleElementReferenceException,ElementClickInterceptedException,ElementClickInterceptedException) as e:
            print('\n更换城市错误 元素错误',e)
            self.err+=1
            if self.err<=5:
                self.click_btn()
            else:
                self.err=0
                del self.driver.requests
                self.getpage()
        except Exception as e:
            print('\n更换城市错误',e)
            #删除本次请求
            del self.driver.requests
            #从头开始重新执行程序
            self.getpage()
        else:
            #若无错误，执行下一步
            self.err=0
            self.getdata()           
            
            
    
    def getdata(self):
        try:
            #等待响应加载完成
            self.predata = self.driver.wait_for_request('/international/search/api/search/batchSearch?.*', timeout=60)
        
            rb=dict(json.loads(self.predata.body).get('flightSegments')[0])
        
        except TimeoutException as e:
            print('\获取数据错误',e)
            #删除本次请求
            del self.driver.requests
            #从头开始重新执行程序
            self.getpage()
        else:
            #检查数据获取正确性
            if rb['departureCityName'] == self.city[0] and rb['arrivalCityName'] == self.city[1]:
                print('城市获取正确')
                #删除本次请求
                del self.driver.requests
                #若无错误，执行下一步
                self.decode_data()
            else:
                #删除本次请求
                del self.driver.requests
                #重新更换城市
                self.changecity()
    
    
    
    def decode_data(self):
        try:
            buf = io.BytesIO(self.predata.response.body)
            gf = gzip.GzipFile(fileobj = buf)
            self.dedata = gf.read().decode('UTF-8')
            self.dedata=json.loads(self.dedata)
        except:
            print('重新获取数据')
            self.getpage()
        else:
            #若无错误，执行下一步
            self.check_data()
            
        
        
    def check_data(self):
        try:
            self.flightItineraryList=self.dedata['data']['flightItineraryList']
            #倒序遍历,删除转机航班
            for i in range(len(self.flightItineraryList)-1, -1, -1):
                if self.flightItineraryList[i]['flightSegments'][0]['transferCount'] !=0:
                    self.flightItineraryList.pop(i)
            if len(self.flightItineraryList):
                #存在直航航班，执行下一步
                self.muti_process()
            else:
                print('不存在直航航班')
                return 0
        except:
            print('不存在直航航班')
            return 0        
                      
    
    def muti_process(self):
        processes = []

        self.flights = pd.DataFrame()
        self.prices = pd.DataFrame()
        #处理航班信息
        processes.append(threading.Thread(target=self.proc_flightSegments))
        #处理票价信息
        processes.append(threading.Thread(target=self.proc_priceList))

        for pro in processes:
            pro.start()
        for pro in processes:
            pro.join()
        
        #若无错误，执行下一步
        self.mergedata()
    
    def proc_flightSegments(self):
        for flightlist in self.flightItineraryList:
            flightlist=flightlist['flightSegments'][0]['flightList']
            flightUnitList=dict(flightlist[0])

            
            departureday=flightUnitList['departureDateTime'].split(' ')[0]
            departuretime=flightUnitList['departureDateTime'].split(' ')[1]
            
            arrivalday=flightUnitList['arrivalDateTime'].split(' ')[0]
            arrivaltime=flightUnitList['arrivalDateTime'].split(' ')[1]            
            
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
            
            
            self.flights=pd.concat([self.flights,pd.DataFrame(flightUnitList,index=[0])],ignore_index=True)

                          
            
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
                discountRate=priceUnitList['discountRate']
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
            
            self.df['数据获取日期']=dt.now().strftime('%Y-%m-%d')
            
            #对pandas的columns进行重命名
            order=['数据获取日期','航班号','航空公司',
                   '出发日期','出发时间','到达日期','到达时间','飞行时长','出发国家','出发城市','出发机场','出发机场三字码',
                   '到达国家','到达城市','到达机场','到达机场三字码','飞机型号','飞机尺寸','飞机型号三字码',
                   '经济舱原价','经济舱最低价','经济舱折扣','商务舱原价','商务舱最低价','商务舱折扣',
                   '到达准点率','停留次数']
            
            origin=['数据获取日期','flightNo','marketAirlineName',
                    'departureday','departuretime','arrivalday','arrivaltime','duration',
                    'departureCountryName','departureCityName','departureAirportName','departureAirportCode',
                    'arrivalCountryName','arrivalCityName','arrivalAirportName','arrivalAirportCode',
                    'aircraftName','aircraftSize','aircraftCode',
                    'economy_origin','economy_low','economy_cut',
                    'bussiness_origin','bussiness_low','bussiness_cut',
                    'arrivalPunctuality','stopCount']
            
            columns=dict(zip(origin,order))

            self.df=self.df.rename(columns=columns)
              
            self.df = self.df[order]
            
            
            if not os.path.exists(self.date):
                os.makedirs(self.date)      

            filename=os.getcwd()+'\\'+self.date+'\\'+self.date+'-'+self.city[0]+'-'+self.city[1]+'.csv'

            self.df.to_csv(filename,encoding='GB18030',index=False)
            
            print('\n数据爬取完成',filename) 
        except Exception as e:
            print('合并数据失败',e)


    def demain(self,citys,citycode):
        self.citycode=citycode
        #设置出发日期
        self.date=dt.now()+timedelta(days=7)
        self.date=self.date.strftime('%Y-%m-%d')
        
        for city in citys:
            self.city=city
            
            if citys.index(city)==0:
                #第一次运行
                self.getpage()
            else:
                #后续运行只需更换出发与目的地
                self.changecity()
        
        #运行结束退出
        self.driver.quit()



if __name__ == '__main__':
    citys=[]
    cityname,citycode=getcitycode()
    city=['上海','广州','深圳','北京']
    ytic=list(reversed(city))
    for m in city:
        for n in ytic:
            if m==n:
                continue
            else:
                citys.append([m,n])
    fly = FLIGHT()
    fly.demain(citys,citycode)
    print('\n程序运行完成！！！！')    
    
