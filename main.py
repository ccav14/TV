import asyncio
from utils.config import config, copy_config
from utils.channel import (
    get_channel_items,
    append_total_data,
    process_sort_channel_list,
    write_channel_to_file,
)
from utils.tools import (
    update_file,
    get_pbar_remaining,
    get_ip_address,
    convert_to_m3u,
    get_result_file_content,
    merge_objects,
)
from updates.subscribe import get_channels_by_subscribe_urls
from updates.multicast import get_channels_by_multicast
from updates.hotel import get_channels_by_hotel
from updates.fofa import get_channels_by_fofa
from updates.online_search import get_channels_by_online_search
import os
from tqdm import tqdm
from tqdm.asyncio import tqdm_asyncio
from time import time
from flask import Flask, render_template_string
import sys
import shutil
from collections import defaultdict

app = Flask(__name__)


@app.route("/")
def show_index():
    return get_result_file_content()


@app.route("/result")
def show_result():
    return get_result_file_content(show_result=True)


@app.route("/log")
def show_log():
    user_log_file = "output/" + (
        "user_result.log" if os.path.exists("config/user_config.ini") else "result.log"
    )
    with open(user_log_file, "r", encoding="utf-8") as file:
        content = file.read()
    return render_template_string(
        "<head><link rel='icon' href='{{ url_for('static', filename='images/favicon.ico') }}' type='image/x-icon'></head><pre>{{ content }}</pre>",
        content=content,
    )


class UpdateSource:

    def __init__(self):
        self.run_ui = False
        self.tasks = []
        self.channel_items = {}
        self.hotel_fofa_result = {}
        self.hotel_tonkiang_result = {}
        self.multicast_result = {}
        self.subscribe_result = {}
        self.online_search_result = {}
        self.channel_data = {}
        self.pbar = None
        self.total = 0
        self.start_time = None
        self.sort_n = 0

    async def visit_page(self, channel_names=None):
        tasks_config = [
            ("open_hotel_fofa", get_channels_by_fofa, "hotel_fofa_result"),
            ("open_multicast", get_channels_by_multicast, "multicast_result"),
            ("open_hotel_tonkiang", get_channels_by_hotel, "hotel_tonkiang_result"),
            ("open_subscribe", get_channels_by_subscribe_urls, "subscribe_result"),
            (
                "open_online_search",
                get_channels_by_online_search,
                "online_search_result",
            ),
        ]

        for setting, task_func, result_attr in tasks_config:
            if (
                setting == "open_hotel_tonkiang" or setting == "open_hotel_fofa"
            ) and config.getboolean("Settings", "open_hotel") == False:
                continue
            if config.getboolean("Settings", setting):
                if setting == "open_subscribe":
                    subscribe_urls = config.get("Settings", "subscribe_urls").split(",")
                    task = asyncio.create_task(
                        task_func(subscribe_urls, callback=self.update_progress)
                    )
                elif setting == "open_hotel_tonkiang" or setting == "open_hotel_fofa":
                    task = asyncio.create_task(task_func(self.update_progress))
                else:
                    task = asyncio.create_task(
                        task_func(channel_names, self.update_progress)
                    )
                self.tasks.append(task)
                setattr(self, result_attr, await task)

    def pbar_update(self, name="", n=0):
        if not n:
            self.pbar.update()
        self.update_progress(
            f"正在进行{name}, 剩余{self.total - (n or self.pbar.n)}个接口, 预计剩余时间: {get_pbar_remaining(n=(n or self.pbar.n), total=self.total, start_time=self.start_time)}",
            int(((n or self.pbar.n) / self.total) * 100),
        )

    def sort_pbar_update(self):
        self.sort_n += 1
        self.pbar_update(name="测速", n=self.sort_n)

    async def main(self):
        try:
            self.channel_items = get_channel_items()
            if self.run_ui:
                copy_config()
            channel_names = [
                name
                for channel_obj in self.channel_items.values()
                for name in channel_obj.keys()
            ]
            await self.visit_page(channel_names)
            self.tasks = []
            channel_items_obj_items = self.channel_items.items()
            self.channel_data = append_total_data(
                channel_items_obj_items,
                self.channel_data,
                self.hotel_fofa_result,
                self.multicast_result,
                self.hotel_tonkiang_result,
                self.subscribe_result,
                self.online_search_result,
            )
            channel_urls = [
                url
                for channel_obj in self.channel_data.values()
                for url_list in channel_obj.values()
                for url in url_list
            ]
            self.total = len(channel_urls)
            if config.getboolean("Settings", "open_sort"):
                self.update_progress(
                    f"正在测速排序, 共{self.total}个接口",
                    0,
                )
                self.start_time = time()
                self.pbar = tqdm_asyncio(total=self.total, desc="Sorting")
                self.sort_n = 0
                self.channel_data = await process_sort_channel_list(
                    self.channel_data, callback=self.sort_pbar_update
                )
            no_result_cate_names = [
                (cate, name)
                for cate, channel_obj in self.channel_data.items()
                for name, info_list in channel_obj.items()
                if len(info_list) < 3
            ]
            no_result_names = [name for (_, name) in no_result_cate_names]
            if no_result_names:
                print(
                    f"Not enough url found for {', '.join(no_result_names)}, try a supplementary multicast search..."
                )
                sup_results = await get_channels_by_multicast(
                    no_result_names, self.update_progress
                )
                sup_channel_items = defaultdict(lambda: defaultdict(list))
                for cate, name in no_result_cate_names:
                    data = sup_results.get(name)
                    if data:
                        sup_channel_items[cate][name] = data
                self.total = len(
                    [
                        url
                        for obj in sup_channel_items.values()
                        for url_list in obj.values()
                        for url in url_list
                    ]
                )
                if self.total > 0 and config.getboolean("Settings", "open_sort"):
                    self.update_progress(
                        f"正在对补充频道测速排序, 共{len([name for obj in sup_channel_items.values() for name in obj.keys()])}个频道, 含{self.total}个接口",
                        0,
                    )
                    self.start_time = time()
                    self.pbar = tqdm_asyncio(total=self.total, desc="Sorting")
                    self.sort_n = 0
                    sup_channel_items = await process_sort_channel_list(
                        sup_channel_items,
                        callback=self.sort_pbar_update,
                    )
                    self.channel_data = merge_objects(
                        self.channel_data, sup_channel_items
                    )
            self.total = len(channel_urls)
            self.pbar = tqdm(total=self.total, desc="Writing")
            self.start_time = time()
            write_channel_to_file(
                channel_items_obj_items,
                self.channel_data,
                callback=lambda: self.pbar_update(name="写入结果"),
            )
            self.pbar.close()
            user_final_file = config.get("Settings", "final_file")
            update_file(user_final_file, "output/result_new.txt")
            if os.path.exists(user_final_file):
                result_file = (
                    "user_result.txt"
                    if os.path.exists("config/user_config.ini")
                    else "result.txt"
                )
                shutil.copy(user_final_file, result_file)
            if config.getboolean("Settings", "open_sort"):
                user_log_file = "output/" + (
                    "user_result.log"
                    if os.path.exists("config/user_config.ini")
                    else "result.log"
                )
                update_file(user_log_file, "output/result_new.log")
            convert_to_m3u()
            print(f"Update completed! Please check the {user_final_file} file!")
            if self.run_ui:
                self.update_progress(
                    f"更新完成, 请检查{user_final_file}文件, 可访问以下链接:",
                    100,
                    True,
                    url=f"{get_ip_address()}",
                )
        except asyncio.exceptions.CancelledError:
            print("Update cancelled!")

    async def start(self, callback=None):
        def default_callback(self, *args, **kwargs):
            pass

        self.update_progress = callback or default_callback
        self.run_ui = True if callback else False
        if config.getboolean("Settings", "open_update"):
            await self.main()
        if self.run_ui and config.getboolean("Settings", "open_update") == False:
            self.update_progress(
                f"服务启动成功, 可访问以下链接:",
                100,
                True,
                url=f"{get_ip_address()}",
            )
            run_app()

    def stop(self):
        for task in self.tasks:
            task.cancel()
        self.tasks = []
        if self.pbar:
            self.pbar.close()


def scheduled_task():
    if config.getboolean("Settings", "open_update"):
        update_source = UpdateSource()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(update_source.start())


def run_app():
    if not os.environ.get("GITHUB_ACTIONS"):
        print(f"You can access the result at {get_ip_address()}")
        app.run(host="0.0.0.0", port=8000)


if __name__ == "__main__":
    if len(sys.argv) == 1 or (len(sys.argv) > 1 and sys.argv[1] == "scheduled_task"):
        scheduled_task()
    run_app()
