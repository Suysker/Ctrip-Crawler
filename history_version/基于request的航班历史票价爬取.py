import requests
import datetime
import re
import demjson
import time
import pandas as pd

def create_assist_date(datestart = None,dateend = None):
	# 创建日期辅助表
	if datestart is None:
		datestart = '2020-01-01'
	if dateend is None:
		dateend = (datetime.datetime.now()+datetime.timedelta(days=-1)).strftime('%Y-%m-%d')

	# 转为日期格式
	datestart=datetime.datetime.strptime(datestart,'%Y-%m-%d')
	dateend=datetime.datetime.strptime(dateend,'%Y-%m-%d')
	date_list = []
	date_list.append(datestart.strftime('%Y-%m-%d'))
	while datestart<dateend:
		# 日期叠加一天
	    datestart+=datetime.timedelta(days=+1)
	    # 日期转字符串存入列表
	    date_list.append(datestart.strftime('%Y-%m-%d'))
	return date_list

def getdata(citys,dateseries):
    url='https://www.lsjpjg.com/getthis.php'
    
    headers={
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Accept-Encoding': 'gzip, deflate, br',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Host': 'www.lsjpjg.com',
        'Origin': 'https://www.lsjpjg.com',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4647.116 Safari/537.36',
        'X-Requested-With': 'XMLHttpRequest'
        }
    
    for city in citys:
        df=pd.DataFrame()
        err=0
    
        for date in dateseries:
        
            data={'dep_dt': date,'dep_ct': city[0],'arr_ct': city[1]}
            res=requests.post(url, headers=headers,data=data)
            #判断航线是否一直不存在
            if res.text=='\ufeff[]' :
                print(city,'无航班',date)
                err+=1
                #数量超过阈值则中断该航线
                if err>30:
                    break
                continue
            else:
                err-=1
                print(city,date)
        
            res.encoding=res.apparent_encoding
            NewResponse = re.sub(r"/","",res.text)
            try:
                r=NewResponse.encode('utf-8')
                j=demjson.decode(r)
            except:
                continue
            temp=pd.DataFrame(j)
            try:
                temp.drop('icon',axis=1,inplace=True)
                temp['出发日期']=date
            except:
                continue
            df=pd.concat([df,temp])
            time.sleep(0.5)
        
        filename=city[0]+'-'+city[1]
        #处理原始数据
        proc_data(filename,df,interval=8)
    

def proc_data(filename,df,interval=8):
    #保存原始数据至本地
    df.to_csv(filename+'.csv',encoding='GB18030')
    df['全票价']=0
    df['日期差']=None
    
    for i in df.index:
        try:
            if not '经济' in df['discount'][i]:
                df.drop(index=i,inplace=True)
            elif '折' in df['discount'][i]:
                #判断出发日期与查询日期之间的间隔是否大于阈值
                delta=datetime.datetime.strptime(df['出发日期'][i],'%Y-%m-%d')-datetime.datetime.strptime(df['qry_dt'][i],'%Y-%m-%d')
                if delta.days >interval:
                    df.drop(index=i,inplace=True)
                    continue
                else:
                    df.loc[i,'日期差']=delta.days
                #通过折扣率计算全票价
                discount=float(re.findall('\d+\.?\d*',df['discount'][i])[0])
                full_price=df['price'][i]/discount*10
                df.loc[i,'全票价']=full_price
            
            elif ('全价'or'经典') in df['discount'][i]:
                #判断出发日期与查询日期之间的间隔是否大于阈值
                delta=datetime.datetime.strptime(df['出发日期'][i],'%Y-%m-%d')-datetime.datetime.strptime(df['qry_dt'][i],'%Y-%m-%d')
                if delta.days >interval:
                    df.drop(index=i,inplace=True)
                    continue
                else:
                    df.loc[i,'日期差']=delta.days
                 #全票价
                full_price=df['price'][i]
                df.loc[i,'全票价']=full_price  
        except:
            df.drop(index=i,inplace=True)
    
    avg_full_price=df[df['全票价']!=0].groupby(['出发日期'])[['全票价']].mean()
    avg_price=df[df['全票价']!=df['price']].groupby(['出发日期'])[['price']].mean()
    result=pd.concat([avg_price,avg_full_price],axis=1)
    
    result['折扣']=result['price']/result['全票价']
    
    #将处理后的数据保存至本地
    result.to_csv(result+'-'+filename+'.csv',encoding='GB18030')
    
 
    
if __name__ == '__main__': 
    citys=[]
    #设置开始与结束日期
    dateseries=create_assist_date(datestart = None,dateend = None)
    
    city=['上海','广州','深圳','北京']
    ytic=list(reversed(city))
    for m in city:
        for n in ytic:
            if m==n:
                continue
            else:
                citys.append([m,n])
    
    getdata(citys,dateseries)