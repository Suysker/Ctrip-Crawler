import pandas as pd
import os
from datetime import datetime, timedelta

def get_departure_destination(file_name):
    name_without_extension = os.path.splitext(file_name)[0]
    return name_without_extension

def merge_csv_files(csv_files, output_xlsx):
    all_dfs = []
    for csv_file in csv_files:
        df = pd.read_csv(csv_file)
        # 添加日期列
        date = os.path.basename(os.path.dirname(os.path.dirname(csv_file)))
        df['出发日期'] = date
        
        # 选择指定的列
        selected_columns = [
            '航班号','出发城市','到达城市', '航空公司', '出发日期', '出发时间', '到达时间', 
            '中转信息', 'economy_origin', '经济舱餐食信息', '经济舱座椅间距', '出发延误时间'
        ]
        df = df[selected_columns]
        
        # 重命名 'economy_origin' 为 '票价'
        df = df.rename(columns={'economy_origin': '票价'})
        
        all_dfs.append(df)
    
    # 合并所有数据框
    merged_df = pd.concat(all_dfs, ignore_index=True)
    
    # 保存为Excel文件
    merged_df.to_excel(output_xlsx, index=False, engine='openpyxl')

# 设置日期范围
start_date = datetime(2024, 11, 1)# 起始日期
end_date = datetime(2024, 11, 4)# 结束日期
clawer_date = datetime(2024, 10, 29)# 爬虫日期
# 设置输入和输出文件夹路径
input_base_path = "./"
output_folder = "./xlsx_output"

# 确保输出文件夹存在
if not os.path.exists(output_folder):
    os.makedirs(output_folder)

# 用于存储同一始发地和目的地的CSV文件
route_files = {}

current_date = start_date
while current_date <= end_date:
    folder_name = current_date.strftime("%Y-%m-%d")
    folder_path = os.path.join(input_base_path, folder_name, clawer_date.strftime("%Y-%m-%d"))
    
    if os.path.exists(folder_path):
        for file_name in os.listdir(folder_path):
            if file_name.endswith('.csv'):
                csv_path = os.path.join(folder_path, file_name)
                route = get_departure_destination(file_name)
                
                if route not in route_files:
                    route_files[route] = []
                route_files[route].append(csv_path)
    
    current_date += timedelta(days=1)

# 合并并保存每个路线的文件
for route, files in route_files.items():
    output_xlsx = os.path.join(output_folder, f"{route}.xlsx")
    merge_csv_files(files, output_xlsx)
    print(f"已合并并保存路线: {route} -> {output_xlsx}")

print("所有CSV文件已成功合并为XLSX文件，并筛选了指定的列")
