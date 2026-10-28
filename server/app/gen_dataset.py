"""Generate 100k high-quality smart home intent dataset for Laya fine-tuning.

Quality rules:
- Each action has 20-70 diverse phrasings: direct commands, polite requests,
  colloquial speech, and indirect intent hints (e.g. "客厅有点暗" -> light_dim).
- Templates with {room} only fill explicit rooms (incl. all->"所有"); bare
  templates use a fixed room override or the action's default room.
- Global dedup: identical text never appears twice; cross-action text
  conflicts are skipped and reported.
"""
import sys
import json
import random
import zlib

sys.stdout.reconfigure(encoding='utf-8')

random.seed(42)

# ============ Questions Definition (single source of truth) ============
QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "What smart home action does the user want to perform?",
        "criteria": {
            "light_on": "turn on lights, open lights, activate lighting",
            "light_off": "turn off lights, close lights, deactivate lighting",
            "light_dim": "adjust light brightness, dim lights, brighten lights",
            "light_color": "change light color, set light color",
            "temp_up": "increase temperature, make it warmer, turn up heat",
            "temp_down": "decrease temperature, make it cooler, turn down heat",
            "temp_set": "set temperature to specific value",
            "curtain_open": "open curtains, open blinds, open shades",
            "curtain_close": "close curtains, close blinds, close shades",
            "music_play": "play music, start music, play songs",
            "music_stop": "stop music, stop playing, pause music",
            "music_next": "next song, skip track",
            "music_prev": "previous song, last track",
            "volume_up": "increase volume, make it louder",
            "volume_down": "decrease volume, make it quieter",
            "volume_set": "set volume to specific level",
            "fan_on": "turn on fan, start fan",
            "fan_off": "turn off fan, stop fan",
            "fan_speed": "adjust fan speed, change fan speed",
            "ac_on": "turn on air conditioner, start AC",
            "ac_off": "turn off air conditioner, stop AC",
            "tv_on": "turn on TV, start television",
            "tv_off": "turn off TV, stop television",
            "scene_sleep": "sleep mode, good night, bedtime scene",
            "scene_movie": "movie mode, cinema scene, film mode",
            "scene_home": "home mode, I'm home, arrive home",
            "scene_away": "away mode, leave home, leaving",
            "scene_party": "party mode, celebration",
            "scene_read": "reading mode, study mode",
            "door_lock": "lock door, lock the door",
            "door_unlock": "unlock door, unlock the door",
            "query_temp": "ask about temperature, what's the temperature",
            "query_humidity": "ask about humidity, what's the humidity",
            "query_weather": "ask about weather, weather forecast",
            "query_time": "ask about time, what time is it",
            "alarm_set": "set alarm, set timer, wake me up",
            "alarm_cancel": "cancel alarm, cancel timer",
            "other": "everything else not covered above"
        }
    },
    "room": {
        "type": "choice",
        "instructions": "Which room is mentioned or implied in the command?",
        "criteria": {
            "living_room": "living room, lounge, 客厅",
            "bedroom": "bedroom, master bedroom, 卧室",
            "kitchen": "kitchen, 厨房",
            "bathroom": "bathroom, washroom, 浴室, 卫生间",
            "study": "study room, home office, 书房",
            "balcony": "balcony, terrace, 阳台",
            "dining_room": "dining room, 餐厅",
            "kids_room": "kids room, children room, 儿童房",
            "all": "all rooms, entire house, everywhere, 全部, 所有",
            "none": "no specific room mentioned or implied"
        }
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent is this request?",
        "criteria": ["not urgent, can wait", "somewhat urgent", "urgent, needs immediate action"]
    }
}

# ============ Templates ============
# Entry: "template with optional {room}/{temp}/{vol}/{time}/{color}"
#     or: ("template", fixed_room_key) for bare templates with explicit room gold

ACTION_TEMPLATES = {
    "light_on": [
        "打开{room}的灯", "开{room}灯", "{room}灯打开", "把{room}的灯开一下",
        "帮我开{room}灯", "我要开{room}灯", "{room}开灯", "请打开{room}灯",
        "{room}的灯开一下", "开一下{room}的灯", "帮我把{room}灯打开",
        "把{room}灯开开", "{room}灯亮起来", "开{room}的灯", "打开{room}灯",
        "{room}灯打开一下", "麻烦开一下{room}灯", "帮我开一下{room}的灯",
        "{room}要开灯", "{room}灯亮一下", "把{room}灯点亮", "{room}照明打开",
        "能不能开下{room}灯", "给{room}开个灯", "{room}灯麻烦开一下",
        "把{room}灯开起来", "{room}的灯亮一下", "开下{room}灯", "{room}灯开下",
        "我要{room}开灯", "{room}灯给开一下", "劳驾开下{room}灯",
        "{room}灯打开吧", "开开{room}灯", "把{room}的灯打开",
        ("把所有灯都打开", "all"), ("全屋的灯都开一下", "all"),
        ("家里灯全打开", "all"), ("所有灯都开开", "all"),
    ],
    "light_off": [
        "关闭{room}的灯", "关{room}灯", "{room}灯关掉", "把{room}的灯关一下",
        "帮我关{room}灯", "我要关{room}灯", "{room}关灯", "请关闭{room}灯",
        "{room}的灯关一下", "关一下{room}的灯", "帮我把{room}灯关掉",
        "把{room}灯关了", "{room}灯灭掉", "关{room}的灯", "关掉{room}灯",
        "{room}灯关闭", "麻烦关一下{room}灯", "帮我关一下{room}的灯",
        "{room}要关灯", "{room}灯灭一下", "把{room}灯关灭", "{room}照明关掉",
        "能不能关下{room}灯", "{room}灯麻烦关一下", "把{room}灯关起来",
        "关下{room}灯", "{room}灯关下", "把{room}的灯关了", "{room}灯关关",
        "{room}灯关掉吧", "帮我把{room}的灯灭了", "{room}的灯别亮了",
        ("把所有灯都关了", "all"), ("全屋的灯都关掉", "all"),
        ("家里灯全关了", "all"), ("所有灯都关一下", "all"), ("关闭所有灯", "all"),
    ],
    "light_dim": [
        "把{room}灯调暗一点", "{room}灯暗一些", "调低{room}灯亮度",
        "{room}灯太亮了", "把{room}灯调亮一点", "{room}灯亮一些",
        "调高{room}灯亮度", "{room}灯太暗了", "{room}灯光柔和一点",
        "{room}灯亮度调整", "把{room}灯调到中等亮度", "{room}灯暗一点",
        "{room}亮点", "{room}太暗了", "{room}有点暗", "把{room}弄亮一点",
        "{room}光线太强了", "{room}光线不够", "把{room}灯调暗",
        "把{room}灯调亮", "{room}灯光调柔和些", "{room}亮度降低一点",
        "{room}亮度提高一点", "{room}灯晃眼", "帮我调下{room}灯的亮度",
        "{room}灯亮度调一下", "把{room}灯光调舒服点", "{room}灯刺眼",
        "把{room}灯调暗些", "把{room}灯调亮些", "{room}看书看不清",
    ],
    "light_color": [
        "把{room}灯换成蓝色", "{room}灯改成暖色", "换{room}灯颜色",
        "把{room}灯调成红色", "{room}灯变绿", "{room}灯改成白色",
        "{room}灯换暖光", "{room}灯调冷光", "把{room}灯调成{color}",
        "{room}灯颜色换成{color}", "把{room}灯变成{color}的",
        "{room}灯给我换{color}", "{room}灯调成{color}看看",
        "把{room}灯换成{color}光", "{room}灯来点{color}", "换{room}灯的光色",
        "{room}灯变{color}", "{room}灯改成{color}光",
    ],
    "temp_up": [
        "把温度调高一点", "太冷了", "温度调高", "热一点", "升温",
        "帮我调高温度", "有点冷", "温度太低了", "调暖一点",
        "空调温度调高", "暖气开大一点", "室温调高一些", "温度升一点",
        "{room}好冷", "{room}有点冷", "把{room}温度调高点", "{room}冷了",
        "温度再高一点", "高一度", "调高两度", "把空调调热一点",
        "好冷啊", "冷死我了", "能不能热点", "{room}温度太低了",
        "把{room}弄暖和点", "取暖", "室温高一点",
    ],
    "temp_down": [
        "把温度调低一点", "太热了", "温度调低", "冷一点", "降温",
        "帮我调低温度", "有点热", "温度太高了", "调凉一点",
        "空调温度调低", "冷气开大一点", "室温调低一些", "温度降一点",
        "{room}好热", "{room}有点热", "把{room}温度调低点", "{room}热了",
        "温度再低一点", "低一度", "调低两度", "把空调调冷一点",
        "好热啊", "热死我了", "能不能凉快点", "{room}温度太高了",
        "把{room}弄凉快点", "制冷", "室温低一点", "来点冷风",
    ],
    "temp_set": [
        "把温度调到{temp}度", "温度设置{temp}度", "调到{temp}度",
        "空调设成{temp}度", "室温调到{temp}度", "空调开到{temp}度",
        "温度定在{temp}度", "调成{temp}度", "空调调到{temp}度",
        "把室温设定{temp}度", "温度{temp}度", "{temp}度就行",
        "帮我调到{temp}度", "室内温度{temp}度", "温度给我设{temp}度",
    ],
    "curtain_open": [
        "打开{room}窗帘", "开{room}窗帘", "{room}窗帘拉开",
        "把{room}窗帘打开", "拉开{room}窗帘", "{room}窗帘开一下",
        "{room}窗帘拉起来", "{room}卷帘打开", "{room}百叶窗打开",
        "把{room}百叶窗升起来", "{room}窗帘开开", "帮我拉开{room}窗帘",
        "{room}窗帘升上去", "开下{room}窗帘", "{room}的窗帘拉上去",
        "麻烦把{room}窗帘打开", "把{room}遮阳帘打开", "{room}要透光",
        "把{room}的百叶窗升上去", "{room}窗帘给拉开",
    ],
    "curtain_close": [
        "关闭{room}窗帘", "关{room}窗帘", "{room}窗帘拉上",
        "把{room}窗帘关上", "拉上{room}窗帘", "{room}窗帘关一下",
        "{room}窗帘拉下来", "{room}卷帘关上", "{room}百叶窗关上",
        "把{room}百叶窗放下来", "{room}窗帘关关", "帮我拉上{room}窗帘",
        "{room}窗帘放下来", "关下{room}窗帘", "{room}的窗帘拉下来",
        "麻烦把{room}窗帘关上", "{room}太晒了", "把{room}遮阳帘放下",
        "把{room}的百叶窗拉下来", "{room}窗帘给拉上", "{room}遮光",
    ],
    "music_play": [
        "播放音乐", "放点歌", "放首歌", "来点音乐", "播放歌曲",
        "我要听歌", "放音乐", "播放一首歌", "放点音乐听", "来首歌",
        "听点音乐", "放个歌听听", "唱首歌", "来点歌声", "放歌",
        "给我放首歌", "听会儿歌", "放点好听的歌", "来点背景音乐",
        "音乐放起来", "我要听音乐", "随机放首歌", "放点流行歌",
        "来首轻音乐", "听歌", "音乐走起", "放音乐听会儿", "想听歌了",
    ],
    "music_stop": [
        "停止播放", "别放了", "音乐关掉", "停止音乐", "暂停音乐",
        "不听了", "关掉音乐", "音乐停一下", "别唱了", "不想听了",
        "把音乐停了", "歌别放了", "音乐暂停", "别放歌了", "音乐关了吧",
        "停一下音乐", "把歌关了", "音乐停掉", "够了别唱了", "别放音乐了",
        "音乐太吵了", "把歌停一下", "歌停了", "停止放歌", "别出声了",
        "音乐声停了", "放完这首别放了", "音乐太难听了别放", "静一下",
    ],
    "music_next": [
        "下一首", "换一首", "切歌", "换下一首", "跳过这首",
        "下一首歌", "换歌", "不要这首", "来下一首", "切下一首",
        "换首歌", "跳过", "这首不好听换一首", "再换一首", "换首别的",
        "不听这首", "下一曲", "随便换一首", "别的歌来一首", "这首跳过去",
    ],
    "music_prev": [
        "上一首", "回到上一首", "放刚才那首", "切回上一首",
        "刚才那首再放", "来上一首", "返回上一首", "放前一首",
        "再放一遍刚才的", "还是上首", "回上一首", "刚才的再来一遍",
        "听上首", "退回上一首", "就前面那首", "上首歌",
    ],
    "volume_up": [
        "声音大一点", "音量调高", "大声一点", "太小声了", "音量开大点",
        "声音调大", "喇叭大声点", "声音太小了", "听不见", "音量大点",
        "搞大声点", "再大声点", "音量加一点", "把声音开大", "音量加两格",
        "声儿大点", "大点声", "响一点", "音量再高些", "声音开大点",
        "音量往上提", "声音提上来", "音量打得大些", "音量往上加",
        "喇叭响些", "音量顶上去", "调大声点", "整个大声点", "音量别这么小",
        "音量提高", "音量加大", "调响一点", "音量弄大点", "提高音量",
        "音量大些", "声音洪亮点", "音量满上", "声儿再大点",
    ],
    "volume_down": [
        "声音小一点", "音量调低", "小声一点", "太大声了", "音量关小点",
        "声音调小", "喇叭小声点", "声音太大了", "太吵了", "音量小点",
        "搞小声点", "再小声点", "音量减一点", "把声音关小", "音量减两格",
        "声儿小点", "小点声", "轻一点", "音量再低些", "声音关小点",
        "音量往下调", "声音压下来", "音量开小些", "音量往下减",
        "喇叭轻些", "音量压低", "调小声点", "整个小声点", "音量别这么大",
        "音量降低", "音量减小", "调轻一点", "音量弄小点", "降低音量",
        "音量小些", "声音轻点", "音量关到最小", "声儿再小点",
    ],
    "volume_set": [
        "音量调到{vol}", "声音设成{vol}", "音量设置{vol}", "音量定在{vol}",
        "音量{vol}", "把音量开到{vol}", "音量调成{vol}", "音量到{vol}就行",
        "音量给我设到{vol}", "声音开到{vol}",
    ],
    "fan_on": [
        "打开{room}风扇", "开{room}风扇", "{room}风扇打开",
        "{room}风扇开一下", "把{room}风扇启动", "{room}风扇转起来",
        "帮我开{room}风扇", "开下{room}风扇", "{room}风扇开开",
        "把{room}风扇开开", "麻烦开下{room}风扇", "{room}的风扇转起来",
        "{room}要吹风", "给{room}开风扇", "{room}风扇开起来",
    ],
    "fan_off": [
        "关闭{room}风扇", "关{room}风扇", "{room}风扇关掉",
        "{room}风扇关一下", "把{room}风扇停了", "{room}风扇停一下",
        "帮我关{room}风扇", "关下{room}风扇", "{room}风扇关关",
        "把{room}风扇关关", "麻烦关下{room}风扇", "{room}的风扇别转了",
        "{room}风扇停掉", "给{room}关风扇",
    ],
    "fan_speed": [
        "风扇调大一点", "风扇调小一点", "风扇转快点", "风扇转慢点",
        "风扇风力调大", "风扇风力调小", "风扇档位调高", "风扇档位调低",
        "风再大点", "风再小点", "风扇快点转", "风扇慢点转", "风扇大风",
        "风扇小风", "风扇风速调一下", "风扇换挡", "风扇调到三档",
        "风扇调到最大", "风扇调到最小", "风力大点", "风力小点",
        "风速加快", "风速放慢", "风扇强劲模式", "风扇轻柔模式",
        "风扇一档", "风扇二档", "风扇三档", "风扇调到二档", "风扇调到一档",
    ],
    "ac_on": [
        "打开空调", "开空调", "空调打开", "开冷气", "开暖气",
        "空调开一下", "把空调启动", "启动空调", "空调开开",
        "帮我开空调", "麻烦开下空调", "把空调打开吧", "空调给开一下",
        "制冷打开", "制热打开", "空调运行", "开一下空调",
        "开下冷空调", "空调制冷开", "空调制热开", "把冷气打开",
        "需要开空调", "空调打开制冷", "来点冷气", "空调开起来",
    ],
    "ac_off": [
        "关闭空调", "关空调", "空调关掉", "关冷气", "空调关一下",
        "把空调关了", "空调停了", "空调关关", "帮我关空调",
        "麻烦关下空调", "空调别开了", "把空调停掉", "关一下空调",
        "空调先关了", "不要开空调了", "空调歇会儿", "把冷气关了",
        "空调停一停", "这会儿不用空调了", "空调关了吧",
    ],
    "tv_on": [
        "打开电视", "开电视", "电视打开", "电视开一下", "把电视启动",
        "开下电视", "电视开开", "帮我开电视", "把电视打开", "电视亮起来",
        "看会儿电视", "电视打开一下", "开电视机", "把电视机打开",
        "看下电视", "想看电视", "电视机开下", "开个电视",
        "要看会儿电视", "电视放起来", "电视开起来",
    ],
    "tv_off": [
        "关闭电视", "关电视", "电视关掉", "电视关一下", "把电视关了",
        "关下电视", "电视关关", "帮我关电视", "别看电视了", "电视机关掉",
        "把电视机关了", "电视关一下", "不看电视了", "电视关了吧",
        "电视别看了", "把电视关一下", "电视停一下", "关掉电视机",
    ],
    "scene_sleep": [
        "晚安", "我要睡觉了", "睡眠模式", "进入睡眠模式", "准备睡觉",
        "开启睡眠模式", "睡觉了", "睡觉模式", "我要休息了", "开启夜间模式",
        "要睡了", "睡觉去了", "进入夜间模式", "该睡觉了", "开启睡眠场景",
        "我困了", "要就寝了", "睡觉模式打开", "我要睡了", "准备就寝",
        "助眠模式", "睡眠场景打开", "开启助眠",
    ],
    "scene_movie": [
        "看电影模式", "影院模式", "电影模式", "我要看电影", "开启影院模式",
        "看电影", "家庭影院模式", "来个观影模式", "打开影院模式",
        "要看电影了", "观影模式", "进入影院模式", "观影模式打开",
        "准备看电影", "今晚看电影", "来个电影氛围", "观影音效模式",
        "电影场景打开", "开启电影场景",
    ],
    "scene_home": [
        "我回来了", "到家了", "回家模式", "开启回家模式", "我到家了",
        "回家了", "到家模式", "进门了", "开启回家场景", "到家模式打开",
        "回来啦", "回家模式打开", "有人回家了", "开回家场景",
        "到家了开模式", "回家场景打开", "开启到家模式", "进屋了",
    ],
    "scene_away": [
        "我出门了", "离家模式", "开启离家模式", "我要走了", "出门了",
        "离开模式", "我走了", "要出门了", "开启离家场景", "走了走了",
        "离家模式打开", "出门模式", "我要外出", "开启出门模式",
        "出去一趟", "全家出门了", "离家场景打开", "外出模式",
    ],
    "scene_party": [
        "派对模式", "聚会模式", "开派对", "party模式", "开启派对模式",
        "来个派对模式", "搞个派对氛围", "聚会场景", "开个聚会模式",
        "派对模式打开", "要开派对了", "准备开派对", "今晚聚会",
        "派对场景打开", "开启聚会模式", "热闹一点的场景", "开个派对氛围",
    ],
    "scene_read": [
        "阅读模式", "看书模式", "读书模式", "学习模式", "工作模式",
        "开启阅读模式", "开启学习模式", "学习场景", "阅读场景打开",
        "要看书了", "开始看书", "读书模式打开", "进入工作模式",
        "进入学习状态", "准备看书", "自习模式", "专注模式", "书房模式",
        "开启专注模式", "学习场景打开",
    ],
    "door_lock": [
        "锁门", "把门锁上", "门锁好", "门锁一下", "帮我锁门",
        "把大门锁了", "锁一下门", "门要锁好", "把门反锁", "门锁上吧",
        "把门锁了", "大门锁一下", "记得锁门", "出门把门锁好",
        "把门带上锁好", "大门反锁", "锁上大门", "门别忘了锁",
        "帮我把门锁好", "门关紧锁上", "进屋了把门锁上",
    ],
    "door_unlock": [
        "开门", "把门开开", "门解锁", "门开一下", "帮我开门",
        "把门打开", "开下门", "门锁打开", "解锁大门", "把锁开开",
        "门开开", "大门开一下", "帮我解锁门", "把门开锁",
        "开下大门", "门锁开一下", "我要进门", "解锁下门",
        "开开门", "把门解锁",
    ],
    "query_temp": [
        "现在多少度", "室内温度多少", "温度多少", "今天多热", "室温多少",
        "家里多少度", "冷不冷", "现在几度", "屋里多少度", "今天几度",
        "室内几度", "现在温度多少度", "家里现在多少度", "家里热不热",
        "室温怎么样", "屋里冷不冷", "现在室温多少", "家里温度多少度",
        "温度几度", "室内温度多少度了", "今天室内多少度", "屋里现在几度",
    ],
    "query_humidity": [
        "湿度多少", "现在湿度", "家里湿度多少", "干燥吗", "家里干不干",
        "湿度是多少", "室内湿度", "现在干燥吗", "家里太干了", "有点干燥",
        "干不干啊", "湿度多少现在", "家里潮不潮", "室内干不干",
        "湿度怎么样", "家里湿度多少度", "现在干不干", "湿度几多",
    ],
    "query_weather": [
        "今天天气怎么样", "天气如何", "外面热不热", "今天下雨吗",
        "明天天气", "今天冷不冷", "外面多少度", "今天天气",
        "外面什么天气", "要下雨吗", "明天冷不冷", "外面天气怎么样",
        "今天外面什么天气", "外面干不干", "明天什么天气", "天气好吗",
        "今天适合出门吗", "外面晒不晒", "天气预报怎么样",
    ],
    "query_time": [
        "现在几点", "几点了", "今天几点", "现在时间", "都几点了",
        "几点啦", "看看几点了", "现在是几点", "告诉我几点", "时间",
        "几点现在", "跟我说下几点", "现在时刻", "都几点", "查下时间",
        "看看时间", "现在几点了", "告诉我时间",
    ],
    "alarm_set": [
        "定{time}的闹钟", "{time}叫我起床", "设一个{time}的闹钟",
        "{time}提醒我", "定个{time}闹钟", "{time}叫我",
        "帮我定{time}的闹钟", "闹钟定在{time}", "{time}的闹钟",
        "定个{time}的铃", "定{time}闹钟", "帮我设{time}的闹钟",
        "要个{time}的闹钟",
    ],
    "alarm_cancel": [
        "取消闹钟", "关掉闹钟", "闹钟取消", "闹钟关掉", "把闹钟取消",
        "不要闹钟了", "闹钟别响了", "撤销闹钟", "明天不用叫我了",
        "闹钟关一下", "取消叫我", "明早不用叫我", "早上别叫我",
        "取消明早的闹钟", "铃取消", "不要叫我了", "把铃关了",
        "撤销闹钟提醒", "闹钟撤销",
    ],
    "other": [
        "帮我查一下快递", "明天有什么安排", "今天星期几", "讲个笑话",
        "有什么新闻", "你叫什么名字", "你会做什么", "今天有什么新闻",
        "明天星期几", "帮我看看日历", "随便聊聊天", "你都会什么",
        "搜索附近的餐厅", "明天行程", "今天日程", "讲个故事",
        "你认识我吗", "自我介绍", "今天新闻摘要", "帮我记件事",
        "开个玩笑", "说个笑话听听", "备忘录里有什么", "最近有什么新鲜事",
    ],
}

ROOMS = {
    "living_room": "客厅",
    "bedroom": "卧室",
    "kitchen": "厨房",
    "bathroom": "卫生间",
    "study": "书房",
    "balcony": "阳台",
    "dining_room": "餐厅",
    "kids_room": "儿童房",
    "all": "所有",
}

EXPLICIT_ROOMS = list(ROOMS.keys())  # for {room} fills (includes all->"所有")

ACTION_DEFAULT_ROOM = {
    "light_on": "none", "light_off": "none", "light_dim": "none",
    "light_color": "none", "temp_up": "none", "temp_down": "none",
    "temp_set": "none", "curtain_open": "none", "curtain_close": "none",
    "music_play": "none", "music_stop": "none", "music_next": "none",
    "music_prev": "none", "volume_up": "none", "volume_down": "none",
    "volume_set": "none", "fan_on": "none", "fan_off": "none",
    "fan_speed": "none", "ac_on": "none", "ac_off": "none",
    "tv_on": "none", "tv_off": "none", "scene_sleep": "bedroom",
    "scene_movie": "living_room", "scene_home": "living_room",
    "scene_away": "all", "scene_party": "living_room",
    "scene_read": "study", "door_lock": "none", "door_unlock": "none",
    "query_temp": "none", "query_humidity": "none", "query_weather": "none",
    "query_time": "none", "alarm_set": "bedroom", "alarm_cancel": "bedroom",
    "other": "none",
}

ACTION_URGENCY = {
    "light_on": 1, "light_off": 1, "light_dim": 0, "light_color": 0,
    "temp_up": 1, "temp_down": 1, "temp_set": 1,
    "curtain_open": 0, "curtain_close": 0,
    "music_play": 0, "music_stop": 0, "music_next": 0, "music_prev": 0,
    "volume_up": 0, "volume_down": 0, "volume_set": 0,
    "fan_on": 1, "fan_off": 1, "fan_speed": 0,
    "ac_on": 1, "ac_off": 1,
    "tv_on": 0, "tv_off": 0,
    "scene_sleep": 0, "scene_movie": 0, "scene_home": 1,
    "scene_away": 1, "scene_party": 0, "scene_read": 0,
    "door_lock": 2, "door_unlock": 2,
    "query_temp": 0, "query_humidity": 0, "query_weather": 0,
    "query_time": 0, "alarm_set": 1, "alarm_cancel": 1,
    "other": 0,
}

COLORS = ["蓝色", "红色", "绿色", "粉色", "紫色", "白色", "暖色", "冷色", "黄色", "橙色"]
TEMPS = [str(t) for t in range(16, 31)]
VOLS = ["10", "20", "30", "50", "60", "70", "80", "90", "一半", "最大", "最小"]
TIMES = ["7点", "6点半", "明早7点", "明早6点", "早上8点", "晚上9点", "晚上10点",
         "7点半", "8点", "下午2点", "中午12点"]


def normalize_entry(entry):
    if isinstance(entry, tuple):
        return entry[0], entry[1]
    return entry, None


def fill_template(text, room_key):
    if "{room}" in text:
        text = text.replace("{room}", ROOMS[room_key])
    text = text.replace("{temp}", random.choice(TEMPS))
    text = text.replace("{vol}", random.choice(VOLS))
    text = text.replace("{time}", random.choice(TIMES))
    text = text.replace("{color}", random.choice(COLORS))
    return text.strip()


# Spoken-language decorations: polite prefixes for command-shaped utterances,
# casual suffixes where they stay natural (no particle clashes, no weird
# interrogative endings). Multiplies phrasing diversity without label noise.
POLITE_PREFIXES = ["帮我", "请", "麻烦", "给我", "劳驾"]
POLITE_WORDS = ("帮我", "麻烦", "请", "劳驾", "给我", "能不能")
COMMAND_START = set("开关打调放播换切锁定设启拉停取加减升降来搞看弄把提顶满")
INTERROGATIVE = ("吗", "多少", "几", "什么", "怎么", "如何", "谁", "哪")


def decorate(text):
    """Return natural spoken variants of a filled template (incl. itself)."""
    prefixes = [""]
    if text and text[0] in COMMAND_START and not any(w in text for w in POLITE_WORDS):
        prefixes = [""] + POLITE_PREFIXES
    if any(w in text for w in INTERROGATIVE) or text.endswith("了"):
        suffixes = ["", "啊", "呀"]
    else:
        suffixes = ["", "吧", "啊", "呀", "呗"]
    if len(prefixes) > 1:
        # polite request + casual 呗/吧 clashes ("请关门呗")
        suffixes = ["", "啊", "呀"]
    return [p + text + s for p in prefixes for s in suffixes]


def urgency_for(text, action):
    """Deterministic per-text urgency: same text always gets the same gold."""
    base = ACTION_URGENCY.get(action, 0)
    jitter = [-1, 0, 0, 1][zlib.crc32(text.encode("utf-8")) % 4]
    return max(0, min(2, base + jitter))


def generate_sample(action):
    entry = random.choice(ACTION_TEMPLATES[action])
    template, fixed_room = normalize_entry(entry)

    if "{room}" in template:
        room_key = random.choice(EXPLICIT_ROOMS)
    elif fixed_room:
        room_key = fixed_room
    else:
        room_key = ACTION_DEFAULT_ROOM[action]

    text = random.choice(decorate(fill_template(template, room_key)))
    urgency = urgency_for(text, action)

    gold = {
        "action": {"probabilities": {action: 1.0}},
        "room": {"probabilities": {room_key: 1.0}},
        "urgency": {"probabilities": {str(urgency): 1.0}},
    }
    return {
        "id": f"sh_{action}_{room_key}_{random.randint(100000, 999999)}",
        "workflow": "smart_home",
        "state": text,
        "gold": gold,
    }


def main(total=100_000):
    n_actions = len(ACTION_TEMPLATES)
    per_action = total // n_actions
    remainder = total - per_action * n_actions

    # Phase 1: generate UNIQUE samples per action (dedup global, no cross-action text)
    uniques = {a: [] for a in ACTION_TEMPLATES}
    seen_text = {}
    cross_conflicts = 0
    dup_skips = 0

    for i, action in enumerate(ACTION_TEMPLATES):
        target = per_action + (1 if i < remainder else 0)
        got = 0
        attempts = 0
        while got < target and attempts < target * 40:
            attempts += 1
            s = generate_sample(action)
            text = s["state"]
            if text in seen_text:
                if seen_text[text] == action:
                    dup_skips += 1
                else:
                    cross_conflicts += 1
                continue
            seen_text[text] = action
            uniques[action].append(s)
            got += 1

    total_unique = sum(len(v) for v in uniques.values())

    # Phase 2: split UNIQUE samples 90/10 (no text leaks train->test)
    train_by_action = {}
    test = []
    for action, samples in uniques.items():
        random.shuffle(samples)
        n_test = max(1, len(samples) // 10) if samples else 0
        test.extend(samples[-n_test:] if n_test else [])
        train_by_action[action] = samples[:-n_test] if n_test else samples

    # Phase 3: oversample TRAIN per action to hit quota (identical gold rows
    # for classes whose unique phrasing space is smaller than the quota)
    train = []
    oversampled = 0
    for i, action in enumerate(ACTION_TEMPLATES):
        target = per_action + (1 if i < remainder else 0)
        train_target = int(target * 0.9)
        pool = train_by_action[action]
        n = len(pool)
        for j in range(train_target):
            if j < n:
                train.append(pool[j])
            else:
                train.append(pool[(j - n) % n])  # duplicate rows, same gold
                oversampled += 1

    random.shuffle(train)
    random.shuffle(test)

    kw = {"ensure_ascii": False, "separators": (",", ":")}
    with open("D:/models/sensevoice-sherpa/smart_home_train.json", "w", encoding="utf-8") as f:
        json.dump(train, f, **kw)
    with open("D:/models/sensevoice-sherpa/smart_home_test.json", "w", encoding="utf-8") as f:
        json.dump(test, f, **kw)

    print(f"Generated total {total_unique} unique samples (target {total})")
    print(f"  Train rows: {len(train)} (of which oversampled duplicates: {oversampled})")
    print(f"  Test rows:  {len(test)} (unique only)")
    print(f"  Duplicate skips during gen: {dup_skips}  Cross-action conflicts: {cross_conflicts}")

    print("\nPer-action: unique / train rows (templates)")
    for a in ACTION_TEMPLATES:
        print(f"  {a:15s} {len(uniques[a]):5d} / {sum(1 for s in train if list(s['gold']['action']['probabilities'])[0] == a):5d}  ({len(ACTION_TEMPLATES[a])})")

    print("\n=== 5 random examples ===")
    for s in random.sample(train, 5):
        g = s["gold"]
        print(f"  '{s['state']}' -> {list(g['action']['probabilities'])[0]} / "
              f"{list(g['room']['probabilities'])[0]} / u={list(g['urgency']['probabilities'])[0]}")


if __name__ == "__main__":
    main()
