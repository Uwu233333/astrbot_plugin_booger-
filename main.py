import asyncio
import json
import random
import time
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from astrbot.api import logger

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.message_components import Image, At, Plain


@register("astrbot_plugin_booger", "小仙", "群友弹鼻屎：游戏菜单、弹射、互擦、排行榜和每日鼻屎王", "1.10.10")
class BoogerPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        # AstrBot 会根据 _conf_schema.json 注入插件配置；保留默认值以兼容旧环境。
        self.config = config if config is not None else {}
        # 使用 AstrBot 规范的插件数据目录：data/plugin_data/astrbot_plugin_booger
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_booger")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "booger_state.json"
        # 兼容旧版本：首次升级时读取插件目录下的旧数据，避免游戏记录丢失。
        self.legacy_data_file = Path(__file__).parent / "data" / "booger_state.json"
        self.state = self._load_state()
        self.cooldown = 30
        self._scheduler_task = None
        self._last_daily_date = ""
        self._last_lucky_date = ""
        self._last_hourly_slot = ""
        self._group_bots = {}
        self._group_umos = {}

    def _load_state(self):
        try:
            source_file = self.data_file
            if not source_file.exists() and self.legacy_data_file.exists():
                source_file = self.legacy_data_file
            if source_file.exists():
                obj = json.loads(source_file.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    obj.setdefault("groups", {})
                    obj.setdefault("cooldowns", {})
                    obj.setdefault("daily", {})
                    # 旧路径的数据成功读取后写入新目录；不删除旧文件，确保迁移可回退。
                    if source_file != self.data_file:
                        self.data_file.write_text(
                            json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                        logger.info("群友弹鼻屎：已将旧版游戏数据迁移到插件数据目录。")
                    return obj
        except Exception:
            pass
        return {"groups": {}, "cooldowns": {}, "daily": {}}

    def _save(self):
        tmp = self.data_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.data_file)

    @staticmethod
    def _gid(event):
        try:
            gid = event.get_group_id()
            return str(gid) if gid else ""
        except Exception:
            return ""

    @staticmethod
    def _uid(event):
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    @staticmethod
    def _name(event):
        try:
            return str(event.get_sender_name() or event.get_sender_id() or "神秘群友")
        except Exception:
            return "神秘群友"

    @staticmethod
    def _message_segments(event):
        try:
            return event.get_messages() or []
        except Exception:
            return []

    @classmethod
    def _at_targets(cls, event):
        """提取消息中的真实 @ 对象，并过滤机器人自身；不要依赖命令文本与 At 段的相对位置。"""
        found = []

        def add_target(value):
            if value is None:
                return
            value = str(value).strip()
            if not value or value.lower() in ("all", "0", "none", "null"):
                return
            if value not in found:
                found.append(value)

        def seg_type(seg):
            typ = getattr(seg, "type", None)
            if isinstance(seg, dict):
                typ = seg.get("type", typ)
            return str(getattr(typ, "value", typ) or "").lower().split(".")[-1]

        def seg_data(seg):
            data = getattr(seg, "data", None)
            if isinstance(seg, dict):
                data = seg.get("data", data)
            return data

        def inspect_at(seg):
            typ = seg_type(seg)
            # AstrBot 不同版本/适配器可能暴露 At、at 或 message_component_type.at。
            if typ != "at" and not typ.endswith("_at"):
                return
            data = seg_data(seg)
            if isinstance(data, dict):
                for key in ("qq", "user_id", "id", "target"):
                    if data.get(key) is not None:
                        add_target(data.get(key))
                        return
            elif data is not None:
                add_target(data)
            else:
                for key in ("qq", "user_id", "id", "target"):
                    value = getattr(seg, key, None)
                    if value is not None:
                        add_target(value)
                        return

        # 先拿机器人自己的 ID；前置唤醒 @ 必须被过滤，而不是作为首个目标。
        bot_ids = set()
        for getter_name in ("get_self_id", "get_bot_id"):
            getter = getattr(event, getter_name, None)
            if callable(getter):
                try:
                    value = getter()
                    if value is not None:
                        bot_ids.add(str(value))
                except Exception:
                    pass
        bot = getattr(event, "bot", None)
        for attr in ("self_id", "id", "uin"):
            value = getattr(bot, attr, None) if bot is not None else None
            if value is not None:
                bot_ids.add(str(value))
        try:
            platform = event.get_platform()
            for attr in ("self_id", "id", "uin"):
                value = getattr(platform, attr, None) if platform is not None else None
                if value is not None:
                    bot_ids.add(str(value))
            getter = getattr(platform, "get_self_id", None)
            if callable(getter):
                value = getter()
                if value is not None:
                    bot_ids.add(str(value))
        except Exception:
            pass

        # 结构化消息链是首选：收集全部 At，之后统一排除机器人自身。
        try:
            for seg in list(event.get_messages() or []):
                inspect_at(seg)
        except Exception:
            logger.exception("[群友弹鼻屎] 读取结构化消息链中的 At 失败")

        # 兼容 OneBot CQ 原始消息及日志常见的 [At:123] 展示格式。
        import re
        raw_values = []
        msg_obj = getattr(event, "message_obj", None)
        for obj in (msg_obj, event):
            if obj is None:
                continue
            for attr in ("raw_message", "message_str"):
                try:
                    value = getattr(obj, attr, None)
                    if isinstance(value, str) and value:
                        raw_values.append(value)
                except Exception:
                    pass
        for value in raw_values:
            for match in re.finditer(r"\[CQ:at,\s*(?:qq|user_id)=(\d+)\]", value, re.I):
                add_target(match.group(1))
            for match in re.finditer(r"\[At:(\d+)\]", value, re.I):
                add_target(match.group(1))

        targets = [uid for uid in found if uid not in bot_ids]
        logger.info("[群友弹鼻屎] @解析: bot_ids=%s all_at=%s targets=%s", sorted(bot_ids), found, targets)
        return targets

    def _group(self, gid):
        return self.state.setdefault("groups", {}).setdefault(
            gid, {"users": {}, "stats": {}, "daily_hits": {}, "daily_date": datetime.now().strftime("%Y-%m-%d"), "activity": {}}
        )

    def _user(self, gid, uid, name=None):
        users = self._group(gid)["users"]
        obj = users.setdefault(uid, {"count": 0, "stink": 0, "name": name or f"群友{uid[-4:]}", "titles": [], "hit_streak": 0})
        obj.setdefault("titles", [])
        obj.setdefault("hit_streak", 0)
        if name:
            obj["name"] = name
        return obj

    def _activity(self, gid, uid):
        """获取群内按日期分桶的统计；日期键用于每日榜与滚动七日榜。"""
        day = datetime.now().strftime("%Y-%m-%d")
        g = self._group(gid)
        daily = g.setdefault("activity", {}).setdefault(day, {})
        return daily.setdefault(str(uid), {"flicks": 0, "hits": 0, "received_hits": 0, "help_accidents": 0, "stink_added": 0})

    def _award_title(self, gid, uid, title):
        user = self._user(gid, uid)
        titles = user.setdefault("titles", [])
        if title not in titles:
            titles.append(title)
            return True
        return False

    def _check_titles(self, gid, uid):
        user = self._user(gid, uid)
        unlocked = []
        count = int(user.get("count", 0))
        stink = int(user.get("stink", 0))
        streak = int(user.get("hit_streak", 0))

        # 鼻屎库存阶梯成就：全部使用带“鼻屎”的称号名，逐级解锁。
        stock_titles = [
            (10, "鼻屎萌新"),
            (50, "鼻屎小贩"),
            (100, "鼻屎收藏家"),
            (250, "鼻屎仓库管理员"),
            (500, "鼻屎批发商"),
            (1000, "鼻屎帝王"),
            (2000, "鼻屎大亨"),
            (5000, "鼻屎领主"),
            (10000, "鼻屎宇宙霸主"),
        ]
        for threshold, title in stock_titles:
            if count >= threshold and self._award_title(gid, uid, title):
                unlocked.append(title)
        if streak >= 5 and self._award_title(gid, uid, "鼻屎连击王"):
            unlocked.append("鼻屎连击王")
        if stink >= 100 and self._award_title(gid, uid, "鼻屎生化炸弹"):
            unlocked.append("鼻屎生化炸弹")
        return unlocked

    def _record_stink_added(self, gid, uid, amount):
        if amount > 0:
            self._activity(gid, uid)["stink_added"] += int(amount)

    def _record_hit(self, gid, target_id, target_name):
        g = self._group(gid)
        today = datetime.now().strftime("%Y-%m-%d")
        if g.get("daily_date") != today:
            g["daily_date"] = today
            g["daily_hits"] = {}
        hits = g.setdefault("daily_hits", {})
        hits[target_id] = int(hits.get(target_id, 0)) + 1
        self._user(gid, target_id, target_name)
        self._activity(gid, target_id)["received_hits"] += 1

    def _add_booger(self, gid, uid, name, amount=1):
        user = self._user(gid, uid, name)
        before = int(user.get("count", 0))
        user["count"] = before + amount
        added_stink = min(100, int(user.get("stink", 0)) + random.randint(15, 25)) - int(user.get("stink", 0))
        user["stink"] = min(100, int(user.get("stink", 0)) + added_stink)
        self._record_stink_added(gid, uid, added_stink)
        return user, before

    @staticmethod
    def _result(event, text):
        return event.plain_result(text)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def _remember_group_session(self, event: AstrMessageEvent):
        """记录真实会话来源和当前 bot；定时任务不能自行拼接 UMO。"""
        gid = self._gid(event)
        if not gid:
            return
        bot = getattr(event, "bot", None)
        if bot is not None:
            self._group_bots[gid] = bot
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            self._group_umos[gid] = str(umo)
            g = self._group(gid)
            if g.get("session_umo") != str(umo):
                g["session_umo"] = str(umo)
                self._save()

    async def _send_game_menu(self, event: AstrMessageEvent):
        self._last_bot = getattr(event, "bot", None)
        # 用真正的图片消息发送菜单，不再依赖 QQ 文本换行渲染。
        menu_path = Path(__file__).parent / "menu.png"
        if not menu_path.is_file():
            yield self._result(event, "菜单图片文件缺失，请重新安装完整插件 ZIP。")
            return
        yield event.chain_result([Image.fromFileSystem(str(menu_path))])

    # 每个命令单独注册。不要把多个 @filter.command 叠在同一个函数上，
    # 否则部分 AstrBot 版本只会识别其中一个命令，另一个触发时就像没反应。
    @filter.command("弹鼻屎游戏")
    async def game_menu(self, event: AstrMessageEvent):
        async for result in self._send_game_menu(event):
            yield result

    @filter.command("弹鼻屎菜单")
    async def game_menu_alias(self, event: AstrMessageEvent):
        async for result in self._send_game_menu(event):
            yield result

    async def _member_name(self, event, gid, target_id):
        """通过群成员资料解析 @ 目标昵称，失败时才使用 ID 后四位兜底。"""
        bot = getattr(event, "bot", None)
        group_value = int(gid) if str(gid).isdigit() else gid
        if bot is not None:
            for method_name in ("get_group_member_info", "get_group_member_profile"):
                method = getattr(bot, method_name, None)
                if callable(method):
                    for kwargs in (
                        {"group_id": group_value, "user_id": int(target_id) if str(target_id).isdigit() else target_id},
                        {"group_id": gid, "user_id": target_id},
                    ):
                        try:
                            result = method(**kwargs)
                            if hasattr(result, "__await__"):
                                result = await result
                            if isinstance(result, dict):
                                result = result.get("data", result.get("result", result))
                                if isinstance(result, dict):
                                    name = result.get("card") or result.get("nickname") or result.get("name")
                                    if name:
                                        return str(name)
                        except Exception:
                            pass
            action = getattr(bot, "call_action", None)
            if callable(action):
                for kwargs in (
                    {"action": "get_group_member_info", "group_id": group_value, "user_id": int(target_id) if str(target_id).isdigit() else target_id},
                    {"action": "get_group_member_info", "group_id": gid, "user_id": target_id},
                ):
                    try:
                        result = action(**kwargs)
                        if hasattr(result, "__await__"):
                            result = await result
                        if isinstance(result, dict):
                            result = result.get("data", result.get("result", result))
                            if isinstance(result, dict):
                                name = result.get("card") or result.get("nickname") or result.get("name")
                                if name:
                                    return str(name)
                    except Exception:
                        pass
        # 如果插件已经保存过该用户的群名，优先使用缓存。
        cached = self._group(gid).get("users", {}).get(str(target_id), {})
        cached_name = cached.get("name")
        if cached_name and not str(cached_name).startswith("群友"):
            return str(cached_name)
        return f"群友{str(target_id)[-4:]}"

    @filter.command("弹鼻屎")
    async def flick(self, event: AstrMessageEvent):
        gid, uid, name = self._gid(event), self._uid(event), self._name(event)
        if not gid:
            yield self._result(event, "🤧 这项运动必须在群里开展，私聊没有观众！")
            return

        targets = self._at_targets(event)
        now = time.time()
        key = f"{gid}:{uid}"
        last = float(self.state.setdefault("cooldowns", {}).get(key, 0))
        remain = self.cooldown - (now - last)
        if remain > 0:
            yield self._result(event, f"🫰 手指正在冷却，还要 {int(remain) + 1} 秒才能再次发射。")
            return

        g = self._group(gid)
        actor = self._user(gid, uid, name)
        stats = g.setdefault("stats", {}).setdefault(uid, {"flicks": 0, "hits": 0})
        stats["flicks"] = int(stats.get("flicks", 0)) + 1
        self._activity(gid, uid)["flicks"] += 1

        # 未 @ 任何人时，与 /随机弹鼻屎 完全相同：先随机抽取真实群友，不再弹空气。
        random_mode = not bool(targets)
        if random_mode:
            picked = await self._random_group_member(event, gid, uid)
            if not picked:
                yield self._result(event, "🎲 暂时拿不到群成员名单，也没有其他已记录的群友可抽选。请先让其他群友参与一次游戏，或检查 QQ 适配器的群成员列表接口。")
                return
            target_id, target_name = picked
        else:
            target_id = targets[0]
            if target_id == uid:
                yield self._result(event, "不能把自己作为指定目标哦。想随机抽其他群友，请直接发送 /弹鼻屎（不用 @ 人）。")
                return
            target_name = await self._member_name(event, gid, target_id)

        self.state["cooldowns"][key] = now
        target = self._user(gid, target_id, target_name)
        lucky = g.get("daily_lucky", {})
        today = datetime.now().strftime("%Y-%m-%d")
        if lucky.get("date") == today and str(lucky.get("uid", "")) == str(target_id):
            self._save()
            yield self._result(event, f"✨ 天选之子护体！{target_name} 是今日天选之子，全天免疫弹鼻屎攻击！")
            return
        roll = random.random()
        if roll < 0.65:
            target, before = self._add_booger(gid, target_id, target_name)
            self._record_hit(gid, target_id, target_name)
            stats["hits"] = int(stats.get("hits", 0)) + 1
            self._activity(gid, uid)["hits"] += 1
            target["hit_streak"] = int(target.get("hit_streak", 0)) + 1
            unlocked = self._check_titles(gid, target_id)
            if unlocked:
                msg_suffix = " 解锁称号：" + "、".join(unlocked) + "！"
            else:
                msg_suffix = ""
            if before == 99:
                msg = f"💥💚 暴击命中！\n╭─ 🎯 命中信息\n│ 攻击者：{name}\n│ 目标：{target_name}\n│ 鼻屎库存：{target['count']} 颗\n╰─ 恶臭值：{target['stink']}/100"
            else:
                msg = random.choice([
                    f"🎯 命中成功！\n╭─ 💩 战报\n│ 攻击者：{name}\n│ 目标：{target_name}\n│ 鼻屎库存：{target['count']} 颗\n╰─ 恶臭值：{target['stink']}/100",
                    f"💥 啪！{target_name} 中招，鼻屎稳稳落在脸上。群里响起了虚假的掌声。",
                    f"🫰 神之一弹！{name} 命中 {target_name}，建议直接报名鼻屎奥运会。当前 {target['count']} 颗。",
                ])
        elif roll < 0.85:
            msg = random.choice([
                f"💨 鼻屎擦着 {target_name} 的耳朵飞过，顺便给发型做了个造型。",
                f"🌬️ {name} 出手！{target_name} 触发闪避，鼻屎只留下了一阵难以描述的风。",
                f"😮 差一点！鼻屎在 {target_name} 面前表演了贴脸飞行，然后扬长而去。",
            ])
        elif roll < 0.95:
            actor, _ = self._add_booger(gid, uid, name)
            msg = f"🤦 反弹！{name} 的鼻屎被命运的后脑勺挡回，自己脸上新增一颗（当前 {actor['count']} 颗）。"
        else:
            msg = f"🎲 鼻屎发生弹道偏移，落入了群聊未知区域。全体成员默契地假装没看见。"
        if 'msg_suffix' in locals():
            msg += msg_suffix
        else:
            if target_id:
                target["hit_streak"] = 0
                self._check_titles(gid, target_id)
        self._save()
        yield self._result(event, msg)

    async def _random_group_member(self, event: AstrMessageEvent, gid: str, sender_id: str):
        """优先从平台获取真实群成员；失败时从本插件已见过的群成员中抽取。"""
        members = []
        bot = getattr(event, "bot", None)
        group_value = int(gid) if str(gid).isdigit() else gid

        # AstrBot/OneBot 不同版本暴露的群成员列表接口略有差异，逐个尝试。
        if bot is not None:
            methods = []
            direct = getattr(bot, "get_group_member_list", None)
            if callable(direct):
                methods.append((direct, [{"group_id": group_value}, {"group_id": gid}, {"group_id": group_value, "no_cache": False}]))
            call_action = getattr(bot, "call_action", None)
            if callable(call_action):
                methods.append((call_action, [
                    {"action": "get_group_member_list", "group_id": group_value},
                    {"action": "get_group_member_list", "group_id": gid},
                ]))
            api = getattr(bot, "api", None)
            api_call = getattr(api, "call_action", None) if api is not None else None
            if callable(api_call):
                methods.append((api_call, [
                    {"action": "get_group_member_list", "group_id": group_value},
                    {"action": "get_group_member_list", "group_id": gid},
                ]))
            for method, variants in methods:
                for kwargs in variants:
                    try:
                        result = method(**kwargs)
                        if hasattr(result, "__await__"):
                            result = await result
                        if isinstance(result, dict):
                            result = result.get("data", result.get("members", result.get("result", result)))
                        if isinstance(result, (list, tuple)):
                            members = list(result)
                            if members:
                                break
                    except Exception:
                        continue
                if members:
                    break

        candidates = {}
        for member in members:
            if isinstance(member, dict):
                mid = member.get("user_id", member.get("qq", member.get("id")))
                mname = member.get("card") or member.get("nickname") or member.get("name")
            else:
                mid = getattr(member, "user_id", getattr(member, "qq", getattr(member, "id", None)))
                mname = getattr(member, "card", None) or getattr(member, "nickname", None) or getattr(member, "name", None)
            if mid is not None and str(mid) != str(sender_id):
                candidates[str(mid)] = str(mname or f"群友{str(mid)[-4:]}")

        # API 不可用时使用已参与过游戏或曾被 @ 的群友，绝不把空气当目标。
        known = self._group(gid).get("users", {})
        for mid, info in known.items():
            if str(mid) != str(sender_id):
                candidates.setdefault(str(mid), str(info.get("name") or f"群友{str(mid)[-4:]}"))
        if not candidates:
            return None
        target_id = random.choice(list(candidates.keys()))
        return target_id, candidates[target_id]

    @filter.command("随机弹鼻屎")
    async def random_flick(self, event: AstrMessageEvent):
        """从真实群成员中随机抽取目标；没有可用成员时明确提示，不会假装命中空气。"""
        gid, uid, name = self._gid(event), self._uid(event), self._name(event)
        if not gid:
            yield self._result(event, "这个玩法只能在群里玩哦。")
            return
        key = f"{gid}:{uid}"
        now = time.time()
        remain = self.cooldown - (now - float(self.state.setdefault("cooldowns", {}).get(key, 0)))
        if remain > 0:
            yield self._result(event, f"🫰 手指冷却中，再等 {int(remain) + 1} 秒。")
            return

        picked = await self._random_group_member(event, gid, uid)
        if not picked:
            yield self._result(event, "🎲 暂时拿不到群成员名单，也没有其他已记录的群友可抽选。请先用 /弹鼻屎 @某人 和群友玩过一次，之后随机模式就能从已记录的群友中抽取。")
            return

        target_id, target_name = picked
        self.state["cooldowns"][key] = now
        self._user(gid, uid, name)
        target = self._user(gid, target_id, target_name)
        g = self._group(gid)
        lucky = g.get("daily_lucky", {})
        today = datetime.now().strftime("%Y-%m-%d")
        if lucky.get("date") == today and str(lucky.get("uid", "")) == str(target_id):
            self._save()
            yield self._result(event, f"✨ 天选之子护体！{target_name} 今日免疫弹鼻屎攻击，随机弹射无效！")
            return
        stats = g.setdefault("stats", {}).setdefault(uid, {"flicks": 0, "hits": 0})
        stats["flicks"] = int(stats.get("flicks", 0)) + 1
        self._activity(gid, uid)["flicks"] += 1

        roll = random.random()
        if roll < 0.65:
            target, before = self._add_booger(gid, target_id, target_name)
            self._record_hit(gid, target_id, target_name)
            stats["hits"] = int(stats.get("hits", 0)) + 1
            self._activity(gid, uid)["hits"] += 1
            target["hit_streak"] = int(target.get("hit_streak", 0)) + 1
            unlocked = self._check_titles(gid, target_id)
            msg_suffix = " 解锁称号：" + "、".join(unlocked) + "！" if unlocked else ""
            if before == 99:
                msg = f"🎲🎯 随机命中！\n╭─ 💩 战报\n│ 攻击者：{name}\n│ 目标：{target_name}\n│ 鼻屎库存：{target['count']} 颗\n╰─ 恶臭值：{target['stink']}/100"
            else:
                msg = random.choice([
                    f"🎲🎯 命运抽中了 {target_name}！{name} 的鼻屎精准命中，对方脸上现在有 {target['count']} 颗。",
                    f"🎯 随机点名成功！{target_name} 躺枪，{name} 获得「群聊投射物」称号。当前 {target['count']} 颗。",
                    f"💥 群友抽奖结果：{target_name}！鼻屎正中目标，恶臭值升至 {target['stink']}/100。",
                ])
        elif roll < 0.85:
            msg = f"💨 随机抽中的目标是 {target_name}！但对方一个闪身，鼻屎擦着耳朵飞过，发型受到轻微惊吓。"
        elif roll < 0.95:
            actor, _ = self._add_booger(gid, uid, name)
            msg = f"🤦 随机目标本来是 {target_name}，没想到鼻屎中途反弹，最后糊回 {name} 自己脸上！当前 {actor['count']} 颗。"
        else:
            msg = f"🌀 随机目标锁定 {target_name}，但鼻屎被一阵神秘气流吹偏——目标躲过一劫，{name} 的投射技术遭到全群质疑。"
        if 'msg_suffix' in locals():
            msg += msg_suffix
        else:
            target["hit_streak"] = 0
            self._check_titles(gid, target_id)
        self._save()
        yield self._result(event, msg)

    @filter.command("擦鼻屎")
    async def wipe(self, event: AstrMessageEvent):
        gid, uid = self._gid(event), self._uid(event)
        user = self._user(gid or "private", uid, self._name(event))
        if int(user.get("count", 0)) <= 0:
            yield self._result(event, "✨ 你脸上干干净净。今天的体面暂时保住了。")
            return
        roll = random.random()
        if roll < 0.70:
            user["count"] = max(0, int(user.get("count", 0)) - 1)
            user["stink"] = max(0, int(user.get("stink", 0)) - 10)
            msg = f"🧻 清理成功！剩余 {user['count']} 颗，恶臭值 {user['stink']}/100。恭喜恢复一点尊严。"
        elif roll < 0.90:
            msg = f"💨 擦歪了！一顿操作后鼻屎还牢牢粘着，仍剩 {user['count']} 颗。再试一次吧（冷静点）。"
        else:
            user["stink"] = min(100, int(user.get("stink", 0)) + 10)
            msg = f"🤢 越擦越糟！不仅没擦掉，恶臭值还 +10，当前 {user['stink']}/100。你的手法值得反思。"
        self._save()
        yield self._result(event, msg)

    @filter.command("帮擦")
    async def help_wipe(self, event: AstrMessageEvent):
        gid, uid, name = self._gid(event), self._uid(event), self._name(event)
        targets = self._at_targets(event)
        if not gid:
            yield self._result(event, "帮擦是群体运动，请回群里操作。")
            return
        if not targets:
            yield self._result(event, "请使用：/帮擦 @某人")
            return
        target_id = targets[0]
        if target_id == uid:
            yield self._result(event, "自己擦自己请用 /擦鼻屎，没必要假装有第三只手。")
            return
        target_name = await self._member_name(event, gid, target_id)
        target = self._user(gid, target_id, target_name)
        actor = self._user(gid, uid, name)
        if int(target.get("count", 0)) <= 0:
            yield self._result(event, f"✨ {target['name']} 干净得像没参加过这个游戏，你的帮助无处发挥。")
            return
        if random.random() < 0.70:
            target["count"] -= 1
            target["stink"] = max(0, int(target.get("stink", 0)) - 10)
            msg = f"🧻 {name} 成功帮 {target['name']} 擦掉一颗！这份友情将被永远记住（直到下次开黑）。"
        else:
            target["count"] -= 1
            actor["count"] = int(actor.get("count", 0)) + 1
            actor["stink"] = min(100, int(actor.get("stink", 0)) + 10)
            self._activity(gid, uid)["help_accidents"] += 1
            self._record_stink_added(gid, uid, 10)
            unlocked = self._check_titles(gid, uid)
            msg = f"💀 帮擦现场发生事故！{name} 手一抖把鼻屎转移到了自己脸上。医药费请自行协商。"
        self._save()
        yield self._result(event, msg)

    @filter.command("烤鼻屎")
    async def roast_booger(self, event: AstrMessageEvent):
        gid = self._gid(event)
        if not gid:
            yield self._result(event, "🍳 鼻屎大餐是群聊厨房，请到群里烹饪。")
            return
        uid, name = self._uid(event), self._name(event)
        user = self._user(gid, uid, name)
        if int(user.get("count", 0)) <= 0:
            yield self._result(event, "🔥 烤箱已经预热，但你没有鼻屎原料！先去收集一颗再来开火。")
            return

        recipes = {
            "普通": ["香烤鼻屎丸", "九分熟鼻屎饼", "脆皮鼻屎串", "蒜香鼻屎粒", "鼻屎蛋包饭"],
            "稀有": ["黄金鼻屎炒饭", "至尊鼻孔披萨", "秘制九转鼻屎", "黑松露鼻屎焗饭", "鼻屎芝士瀑布"],
            "史诗": ["七彩鼻屎圣代", "鼻孔核聚变烧烤", "银河系鼻屎火锅", "鼻屎陨石寿司", "臭味龙卷风拉面"],
            "传说": ["盘古开天鼻屎宴", "玉皇大帝私房鼻屎", "宇宙终极臭味佛跳墙", "诸天万界鼻屎满汉全席", "鼻屎创世神套餐"],
        }
        roll = random.random()
        quality = "普通" if roll < 0.60 else "稀有" if roll < 0.85 else "史诗" if roll < 0.97 else "传说"
        dish = {
            "id": f"{int(time.time() * 1000)}-{random.randint(1000, 9999)}",
            "name": random.choice(recipes[quality]),
            "quality": quality,
            "chef_id": str(uid),
            "chef_name": str(name),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        user["count"] = int(user.get("count", 0)) - 1
        g = self._group(gid)
        dishes = g.setdefault("dishes", [])
        dishes.append(dish)
        self._save()
        quality_emoji = {"普通": "🥔", "稀有": "🥇", "史诗": "🌈", "传说": "👑"}[quality]
        lines = [
            "🍳【新鲜出炉！鼻屎大餐】",
            "━━━━━━━━━━━━━━",
            f"🍽️ 菜名：{dish['name']}",
            f"{quality_emoji} 品质：{quality}",
            f"👨‍🍳 主厨：{name}",
            "🥢 原料消耗：1 颗鼻屎",
            f"📦 剩余原料：{user['count']} 颗",
            "",
            "📋 料理已加入本群大餐！",
            "输入 /鼻屎大餐 查看，输入 /吃鼻屎 开吃。",
        ]
        yield self._result(event, "\n".join(lines))

    @filter.command("鼻屎大餐")
    async def booger_feast(self, event: AstrMessageEvent):
        gid = self._gid(event)
        if not gid:
            yield self._result(event, "🍽️ 鼻屎大餐只统计群聊厨房，请到群里查看。")
            return
        g = self._group(gid)
        dishes = list(g.get("dishes", []))
        if not dishes:
            yield self._result(event, "🍽️【本群鼻屎大餐】\n\n📦 当前待享用：0 道\n\n━━━━━━━━━━━━━━\n\n🍳 厨房空空如也！\n快使用「/烤鼻屎」制作第一道黑暗料理。")
            return
        icons = {"普通": "🥔", "稀有": "🥇", "史诗": "🌈", "传说": "👑"}
        lines = [
            "🍽️【本群鼻屎大餐】",
            "",
            f"📦 当前待享用：{len(dishes)} 道",
            "",
            "━━━━━━━━━━━━━━",
            "",
            "📚【品质统计】",
        ]
        # 展示最近 15 道；品质统计覆盖全部库存。每项单独一行，避免 QQ 横向挤压。
        counts = {q: sum(1 for d in dishes if d.get("quality") == q) for q in ("普通", "稀有", "史诗", "传说")}
        visible_counts = [f"{icons[q]} {q}料理：{counts[q]} 道" for q in counts if counts[q]]
        lines.extend(visible_counts or ["目前还没有料理。"])
        lines.extend(["", "━━━━━━━━━━━━━━", "", "🥢【新鲜出炉】", ""])
        for i, dish in enumerate(reversed(dishes[-15:]), 1):
            quality = dish.get("quality", "普通")
            lines.extend([
                f"{i:02d}. {icons.get(quality, '🍴')} {dish.get('name', '神秘鼻屎料理')}",
                f"   👨‍🍳 主厨：{dish.get('chef_name', '匿名大厨')}",
                f"   ✨ 品质：{quality}",
                "",
            ])
        if len(dishes) > 15:
            lines.extend([f"……另有 {len(dishes) - 15} 道料理暂未展示。", ""])
        lines.extend(["━━━━━━━━━━━━━━", "", "🎲 输入「/吃鼻屎」", "随机吃掉一道料理！", "", "祝你吃得开心……如果还能开心的话。💩"])
        yield self._result(event, "\n".join(lines))

    @filter.command("吃鼻屎")
    async def eat_booger(self, event: AstrMessageEvent):
        gid = self._gid(event)
        if not gid:
            yield self._result(event, "🍽️ 这套鼻屎大餐属于群聊厨房，请到群里享用。")
            return
        uid, name = self._uid(event), self._name(event)
        g = self._group(gid)
        dishes = g.setdefault("dishes", [])
        if not dishes:
            yield self._result(event, "🍽️【厨房空空如也】\n没有现成料理可以吃！\n快使用 /烤鼻屎，给大家做一道新菜吧。")
            return
        index = random.randrange(len(dishes))
        dish = dishes.pop(index)
        user = self._user(gid, uid, name)
        effects = [
            ("清新口气", "恶臭值降低 10 点", lambda: user.__setitem__("stink", max(0, int(user.get("stink", 0)) - 10))),
            ("鼻屎补脑", "额外获得 1 颗鼻屎库存", lambda: user.__setitem__("count", int(user.get("count", 0)) + 1)),
            ("消化系统觉醒", "获得 2 颗鼻屎库存", lambda: user.__setitem__("count", int(user.get("count", 0)) + 2)),
            ("鼻孔超频", "恶臭值降低 5 点，鼻孔仿佛涡轮增压", lambda: user.__setitem__("stink", max(0, int(user.get("stink", 0)) - 5))),
            ("臭气反噬", "恶臭值增加 10 点", lambda: user.__setitem__("stink", min(100, int(user.get("stink", 0)) + 10))),
            ("厨神认证", "额外获得 1 颗鼻屎库存", lambda: user.__setitem__("count", int(user.get("count", 0)) + 1)),
        ]
        effect = random.choice(effects)
        effect[2]()
        user["stink"] = max(0, min(100, int(user.get("stink", 0))))
        self._check_titles(gid, uid)
        chef_id = str(dish.get("chef_id", ""))
        chef_name = str(dish.get("chef_name", "匿名大厨"))
        chef_reward = False
        if chef_id and chef_id != str(uid):
            chef = self._user(gid, chef_id, chef_name)
            chef["count"] = int(chef.get("count", 0)) + 1
            chef_reward = True
        self._save()
        quality_icon = {"普通": "🥔", "稀有": "🥇", "史诗": "🌈", "传说": "👑"}.get(dish.get("quality"), "🍴")
        lines = [
            "🍽️【鼻屎大餐·试吃报告】",
            "━━━━━━━━━━━━━━",
            f"😋 食客：{name}",
            f"{quality_icon} 菜品：{dish.get('name', '神秘鼻屎料理')}",
            f"✨ 品质：{dish.get('quality', '普通')}",
            f"👨‍🍳 制作人：{chef_name}",
            "",
            f"🎲【食用效果】{effect[0]}",
            f"　{effect[1]}",
        ]
        if chef_reward:
            lines.append(f"🎁 主厨奖励：{chef_name} 获得 1 颗鼻屎，因为料理被别人吃掉了！")
        lines.extend(["", f"☣️ 当前恶臭值：{user.get('stink', 0)}/100", f"📦 剩余料理：{len(dishes)} 道", "━━━━━━━━━━━━━━"])
        yield self._result(event, "\n".join(lines))

    @filter.command("鼻屎榜")
    async def leaderboard(self, event: AstrMessageEvent):
        gid = self._gid(event) or "private"
        g = self._group(gid)
        users, stats = g.get("users", {}), g.get("stats", {})
        if not users:
            yield self._result(event, "榜单暂时空空如也。使用 /弹鼻屎 或 /随机弹鼻屎 开始制造历史。")
            return
        messy = sorted(users.items(), key=lambda x: (int(x[1].get("count", 0)), int(x[1].get("stink", 0))), reverse=True)[:5]
        accurate = sorted(stats.items(), key=lambda x: (int(x[1].get("hits", 0)), int(x[1].get("flicks", 0))), reverse=True)[:5]
        lines = ["🏆 本群鼻屎榜", "【脸上最热闹】"]
        lines += [f"{i}. {u.get('name', '神秘群友')}：{u.get('count', 0)} 颗｜恶臭 {u.get('stink', 0)}/100" for i, (_, u) in enumerate(messy, 1)]
        lines.append("【弹得最准】")
        lines += [f"{i}. {users.get(uid, {}).get('name', '神秘群友')}：命中 {s.get('hits', 0)} 次 / 出手 {s.get('flicks', 0)} 次" for i, (uid, s) in enumerate(accurate, 1)]
        yield self._result(event, "\n".join(lines))

    @filter.command("鼻屎状态")
    async def status(self, event: AstrMessageEvent):
        gid, uid = self._gid(event) or "private", self._uid(event)
        user = self._user(gid, uid, self._name(event))
        titles = user.get("titles", [])
        title_text = "、".join(titles) if titles else "暂无称号"
        yield self._result(event, f"╭─ 🪞 个人鼻屎档案\n│ 👤 玩家：{user.get('name', self._name(event))}\n│ 💩 鼻屎库存：{user.get('count', 0)} 颗\n│ ☣️ 恶臭值：{user.get('stink', 0)}/100\n╰─ 🏅 称号：{title_text}")

    @filter.command("鼻屎成就")
    async def achievements(self, event: AstrMessageEvent):
        gid, uid = self._gid(event) or "private", self._uid(event)
        user = self._user(gid, uid, self._name(event))
        self._check_titles(gid, uid)
        titles = user.get("titles", [])
        descriptions = [
            "🌱 鼻屎萌新：库存达到 10 颗",
            "🛒 鼻屎小贩：库存达到 50 颗",
            "🧺 鼻屎收藏家：库存达到 100 颗",
            "📦 鼻屎仓库管理员：库存达到 250 颗",
            "🚚 鼻屎批发商：库存达到 500 颗",
            "👑 鼻屎帝王：库存达到 1000 颗",
            "💰 鼻屎大亨：库存达到 2000 颗",
            "🏰 鼻屎领主：库存达到 5000 颗",
            "🌌 鼻屎宇宙霸主：库存达到 10000 颗",
            "🎯 鼻屎连击王：连续被命中 5 次",
            "☣️ 鼻屎生化炸弹：恶臭值达到 100",
        ]
        unlocked = "、".join(titles) if titles else "暂未解锁"
        unlocked_set = set(titles)
        self._save()
        # 动态成就图：保留原始美术，只在已解锁项目上画绿色勾，并更新顶部解锁摘要。
        image_path = Path(__file__).parent / "achievements.png"
        if image_path.is_file():
            try:
                from PIL import Image as PILImage, ImageDraw, ImageFont
                base = PILImage.open(image_path).convert("RGB")
                draw = ImageDraw.Draw(base)
                font_candidates = [
                    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                ]
                font = None
                for font_path in font_candidates:
                    if Path(font_path).exists():
                        try:
                            font = ImageFont.truetype(font_path, 19)
                            break
                        except Exception:
                            pass
                if font is None:
                    font = ImageFont.load_default()

                # 原图顶部“已解锁：暂未解锁”文字区域，用底色覆盖后绘制实时状态。
                draw.rounded_rectangle((510, 222, 850, 266), radius=8, fill=(255, 246, 218))
                if titles:
                    # 用最高阶库存称号优先展示，避免长称号列表挤出图片区域。
                    stock_order = ["鼻屎萌新", "鼻屎小贩", "鼻屎收藏家", "鼻屎仓库管理员", "鼻屎批发商", "鼻屎帝王", "鼻屎大亨", "鼻屎领主", "鼻屎宇宙霸主"]
                    best_title = next((t for t in reversed(stock_order) if t in titles), titles[-1])
                    status_text = f"已解锁：{best_title}等{len(titles)}项"
                else:
                    status_text = "已解锁：暂未解锁"
                # 自动缩小字体，确保摘要始终位于横幅内。
                size = 19
                while size > 12:
                    try:
                        status_font = ImageFont.truetype(next((fp for fp in font_candidates if Path(fp).exists()), font_candidates[-1]), size)
                    except Exception:
                        status_font = font
                    if draw.textbbox((0, 0), status_text, font=status_font)[2] <= 330:
                        break
                    size -= 1
                draw.text((522, 231), status_text, font=status_font if 'status_font' in locals() else font, fill=(63, 55, 40))

                # 成就图中 11 个圆圈对应列表顺序；已解锁者绘制醒目的绿色勾。
                achievement_titles = [
                    "鼻屎萌新", "鼻屎小贩", "鼻屎收藏家", "鼻屎仓库管理员",
                    "鼻屎批发商", "鼻屎帝王", "鼻屎大亨", "鼻屎领主",
                    "鼻屎宇宙霸主", "鼻屎连击王", "鼻屎生化炸弹",
                ]
                row_y = [469, 550, 630, 710, 790, 870, 951, 1031, 1112, 1192, 1274]
                unlocked_set = set(titles)
                for title, cy in zip(achievement_titles, row_y):
                    if title in unlocked_set:
                        # 覆盖原空心圆内部，再绘制绿色勾，保留黑灰外圈。
                        draw.ellipse((917, cy - 12, 943, cy + 12), fill=(222, 246, 201))
                        draw.line((922, cy, 929, cy + 7, 940, cy - 7), fill=(54, 150, 55), width=5, joint="curve")

                # 每次请求使用独立文件，避免不同用户同时查询时互相覆盖。
                generated = Path(tempfile.gettempdir()) / f"booger_achievements_{gid}_{uid}_{int(time.time() * 1000)}.png"
                base.save(generated, format="PNG", optimize=True)
                yield event.chain_result([Image.fromFileSystem(str(generated))])
                summary = "🏅【你的鼻屎成就】\n━━━━━━━━━━━━━━\n✨ 已解锁：" + unlocked + f"\n📊 已解锁 {len(titles)} / {len(descriptions)} 项"
                yield self._result(event, summary)
                return
            except Exception as exc:
                logger.warning(f"动态成就图片生成失败，改用原图：{exc}")
                yield event.chain_result([Image.fromFileSystem(str(image_path))])
                summary = "🏅【你的鼻屎成就】\n━━━━━━━━━━━━━━\n✨ 已解锁：" + unlocked + f"\n📊 已解锁 {len(titles)} / {len(descriptions)} 项"
                yield self._result(event, summary)
                return
        # 兼容缺少图片文件的旧安装：退回文字版。
        lines = [
            "🏅【鼻屎成就档案】",
            "━━━━━━━━━━━━━━",
            f"✨ 已解锁：{unlocked}",
            "",
            "📜【成就收集册】",
            "━━━━━━━━━━━━━━",
        ]
        for item in descriptions:
            title = item.split("：", 1)[0].split(" ", 1)[-1]
            mark = "✅" if title in unlocked_set else "▫️"
            lines.append(f"{mark} {item}")
        lines.extend([
            "",
            "━━━━━━━━━━━━━━",
            "💡 库存类称号会随鼻屎库存达标解锁。",
            "🌟 继续收集，冲击更高阶的鼻屎称号！",
        ])
        yield self._result(event, "\n".join(lines))

    @filter.command("今日神射手")
    async def daily_sharpshooter(self, event: AstrMessageEvent):
        gid = self._gid(event) or "private"
        g = self._group(gid)
        today = datetime.now().strftime("%Y-%m-%d")
        rows = [(uid, v) for uid, v in g.get("activity", {}).get(today, {}).items() if int(v.get("flicks", 0)) >= 3]
        if not rows:
            yield self._result(event, "🎯 今日神射手暂时空缺；至少出手 3 次才参与命中率排名。")
            return
        uid, v = max(rows, key=lambda item: (int(item[1].get("hits", 0)) / max(1, int(item[1].get("flicks", 0))), int(item[1].get("hits", 0))))
        rate = int(v.get("hits", 0)) / max(1, int(v.get("flicks", 0))) * 100
        name = self._user(gid, uid).get("name", f"群友{uid[-4:]}")
        yield self._result(event, f"🎯 今日神射手：{name}！出手 {v['flicks']} 次，命中 {v['hits']} 次，命中率 {rate:.1f}%。")

    @filter.command("今日背锅侠")
    async def daily_scapegoat(self, event: AstrMessageEvent):
        gid = self._gid(event) or "private"
        g = self._group(gid)
        today = datetime.now().strftime("%Y-%m-%d")
        rows = [(uid, v) for uid, v in g.get("activity", {}).get(today, {}).items() if int(v.get("help_accidents", 0)) > 0]
        if not rows:
            yield self._result(event, "🧻 今日背锅侠暂时空缺，今天的帮擦还没有发生转移事故。")
            return
        uid, v = max(rows, key=lambda item: int(item[1].get("help_accidents", 0)))
        name = self._user(gid, uid).get("name", f"群友{uid[-4:]}")
        yield self._result(event, f"💀 今日背锅侠：{name}！今天帮擦转移事故 {v['help_accidents']} 次。")

    @filter.command("本周最臭")
    async def weekly_stink(self, event: AstrMessageEvent):
        gid = self._gid(event) or "private"
        g = self._group(gid)
        today = datetime.now().date()
        totals = {}
        for day_key, players in g.get("activity", {}).items():
            try:
                age = (today - datetime.strptime(day_key, "%Y-%m-%d").date()).days
            except ValueError:
                continue
            if 0 <= age < 7:
                for uid, values in players.items():
                    totals[uid] = totals.get(uid, 0) + int(values.get("stink_added", 0))
        if not totals or max(totals.values(), default=0) <= 0:
            yield self._result(event, "🤧 本周最臭暂时空缺，大家还没有积累本周恶臭记录。")
            return
        uid, points = max(totals.items(), key=lambda item: item[1])
        name = self._user(gid, uid).get("name", f"群友{uid[-4:]}")
        yield self._result(event, f"☣️ 本周最臭：{name}！近 7 天累计增加恶臭值 {points} 点。")

    @filter.command("整点播报")
    async def hourly_toggle(self, event: AstrMessageEvent):
        """按群开关每小时整点随机弹射播报：/整点播报 开 或 /整点播报 关。"""
        gid = self._gid(event)
        self._last_bot = getattr(event, "bot", None)
        if not gid:
            yield self._result(event, "请在群聊中使用：/整点播报 开 或 /整点播报 关")
            return
        raw = str(getattr(event, "message_str", "") or "").strip()
        # 不同 AstrBot 版本 message_str 可能包含或不包含命令前缀，取最后一个词作为参数。
        arg = raw.split()[-1].strip().lower() if raw.split() else ""
        g = self._group(gid)
        toggle_on = arg in ("开", "开启", "on", "打开", "启用")
        toggle_off = arg in ("关", "关闭", "off", "停用")
        if toggle_on or toggle_off:
            # QQ 适配器有时不会把真实群角色放进 event；必要时主动查询群成员资料。
            owner_roles = {"owner", "群主", "群组拥有者", "group_owner"}
            role = ""
            try:
                role = str(event.get_sender_role() or "").strip().lower()
            except Exception:
                pass
            if role not in owner_roles:
                try:
                    sender = event.get_sender()
                    if isinstance(sender, dict):
                        role = str(sender.get("role", role) or role).strip().lower()
                    elif sender is not None:
                        role = str(getattr(sender, "role", role) or role).strip().lower()
                except Exception:
                    pass
            if role not in owner_roles:
                bot = getattr(event, "bot", None)
                api = getattr(bot, "api", None) if bot is not None else None
                uid = self._uid(event)
                group_value = int(gid) if str(gid).isdigit() else gid
                user_value = int(uid) if str(uid).isdigit() else uid
                methods = []
                if bot is not None:
                    direct = getattr(bot, "get_group_member_info", None)
                    if callable(direct):
                        methods.append((direct, [
                            ((), {"group_id": group_value, "user_id": user_value}),
                            ((), {"group_id": gid, "user_id": uid}),
                        ]))
                    for obj in (bot, api):
                        action = getattr(obj, "call_action", None) if obj is not None else None
                        if callable(action):
                            methods.append((action, [
                                (("get_group_member_info",), {"group_id": group_value, "user_id": user_value}),
                                (("get_group_member_info",), {"group_id": gid, "user_id": uid}),
                                ((), {"action": "get_group_member_info", "group_id": group_value, "user_id": user_value}),
                            ]))
                for method, variants in methods:
                    for args, kwargs in variants:
                        try:
                            result = method(*args, **kwargs)
                            if hasattr(result, "__await__"):
                                result = await result
                            if isinstance(result, dict):
                                result = result.get("data", result.get("result", result))
                            if isinstance(result, dict):
                                role = str(result.get("role", "") or "").strip().lower()
                                if role:
                                    break
                        except Exception:
                            continue
                    if role in owner_roles:
                        break
            if role not in owner_roles:
                yield self._result(event, "🔒 无法确认你的群主身份，或你不是本群群主。请确认 QQ 适配器支持查询群成员资料后重试。")
                return
        if toggle_on:
            g["hourly_enabled"] = True
            self._save()
            yield self._result(event, "⏰ 本群每小时整点随机弹射播报已开启。")
        elif toggle_off:
            g["hourly_enabled"] = False
            self._save()
            yield self._result(event, "🔕 本群每小时整点随机弹射播报已关闭。")
        else:
            enabled = g.get("hourly_enabled", True)
            yield self._result(event, f"本群每小时整点播报当前为：{'开启' if enabled else '关闭'}。用法：/整点播报 开 或 /整点播报 关")

    @filter.command("今日天选之子")
    async def daily_lucky(self, event: AstrMessageEvent):
        """查询本群今天的天选之子；若当天尚未抽选则补选，但只回复当前查询，不额外群发公告。"""
        gid = self._gid(event)
        if not gid:
            yield self._result(event, "✨ 天选之子只在群聊中产生哦。")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        g = self._group(gid)
        lucky = g.get("daily_lucky", {})
        if lucky.get("date") != today:
            # 先缓存当前消息的 bot，再请求群成员列表；此前顺序会导致首次查询必定拿不到 bot。
            bot = getattr(event, "bot", None)
            if bot is not None:
                self._group_bots[str(gid)] = bot
            picked = await self._random_group_member_for_lucky(gid)
            if not picked:
                yield self._result(event, "✨ 今日天选之子尚未选出。当前无法获取群成员名单，请确认 QQ 适配器支持获取群成员列表后再试。")
                return
            uid, name = picked
            lucky = {"date": today, "uid": str(uid), "name": str(name)}
            g["daily_lucky"] = lucky
            self._save()
        else:
            name, uid = lucky.get("name", "神秘群友"), lucky.get("uid", "未知")
        yield self._result(event, f"✨ 今日天选之子\n\n👑 幸运儿：{name}\n🆔 QQ：{uid}\n🛡️ 今日效果：免疫弹鼻屎攻击")

    @filter.command("今日鼻屎王")
    async def daily_king(self, event: AstrMessageEvent):
        gid = self._gid(event) or "private"
        g = self._group(gid)
        today = datetime.now().strftime("%Y-%m-%d")
        hits = g.get("daily_hits", {}) if g.get("daily_date") == today else {}
        if not hits:
            yield self._result(event, "👑 今日鼻屎王暂时空缺——大家今天都还挺干净（或是弹得太菜）。")
            return
        uid, count = max(hits.items(), key=lambda x: int(x[1]))
        name = self._user(gid, uid).get("name", f"群友{uid[-4:]}")
        yield self._result(event, f"👑 今日鼻屎王：{name}！今天累计被精准命中 {count} 次。请发表获奖感言，并领取虚拟纸巾一包。")

    async def _announcement_loop(self):
        """每分钟检查一次：整点群内随机弹射、每天 23:00 公布鼻屎王。"""
        while True:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")

            # 每天 00:00 选出天选之子；该播报不受免打扰时段影响。
            if now.hour == 0 and now.minute == 0 and self._last_lucky_date != today:
                self._last_lucky_date = today
                for gid in list(self.state.get("groups", {}).keys()):
                    if not gid or gid == "private":
                        continue
                    try:
                        picked = await self._random_group_member_for_lucky(gid)
                        if picked:
                            uid, name = picked
                            g = self._group(gid)
                            g["daily_lucky"] = {"date": today, "uid": str(uid), "name": str(name)}
                            await self._send_group(gid, f"✨【今日天选之子】恭喜 {name}（{uid}）！从现在起至今日结束，免疫所有弹鼻屎攻击！本播报不受免打扰时段影响。")
                        else:
                            logger.warning("群 %s 暂时无法选出今日天选之子：无法获取群成员", gid)
                    except Exception as exc:
                        logger.warning("群 %s 天选之子播报失败：%s", gid, exc)
                self._save()

            # 每个小时只触发一次，插件重载后不会在同一小时重复触发。
            slot = now.strftime("%Y-%m-%d-%H")
            if now.minute == 0 and self._last_hourly_slot != slot:
                self._last_hourly_slot = slot
                if self._is_quiet_time(now):
                    logger.info("当前处于免打扰时段，跳过整点播报：%s", now.strftime("%H:%M"))
                else:
                    for gid in list(self.state.get("groups", {}).keys()):
                        if not gid or gid == "private":
                            continue
                        try:
                            await self._hourly_random_flick(gid)
                        except Exception as exc:
                            logger.exception("整点随机弹射失败，群 %s：%s", gid, exc)
                            try:
                                await self._send_group(gid, f"⏰【整点随机弹射】本次执行出错：{type(exc).__name__}。请查看 AstrBot 日志。")
                            except Exception as send_exc:
                                logger.warning("整点错误提示也未能发送，群 %s：%s", gid, send_exc)

            # 每天 23:00 公布今日鼻屎王；免打扰时段内同样跳过。
            if now.hour == 23 and now.minute == 0 and self._last_daily_date != today:
                self._last_daily_date = today
                # 每日鼻屎王颁奖属于固定播报，不受免打扰时段影响。
                for gid in list(self.state.get("groups", {}).keys()):
                    if not gid or gid == "private":
                        continue
                    try:
                        g = self._group(gid)
                        hits = g.get("daily_hits", {}) if g.get("daily_date") == today else {}
                        if hits:
                            uid, count = max(hits.items(), key=lambda x: int(x[1]))
                            name = self._user(gid, uid).get("name", f"群友{str(uid)[-4:]}")
                            message = f"🌙【今日鼻屎王颁奖典礼】恭喜 {name}！今天被精准命中 {count} 次，荣获今日鼻屎王！奖品：全群敬而远之三秒钟。"
                        else:
                            message = "🌙【今日鼻屎王颁奖典礼】今日无人中选！本群保持了难得的卫生纪录，明天继续争夺（？）"
                        await self._send_group(gid, message)
                    except Exception as exc:
                        logger.warning("鼻屎王播报失败，群 %s：%s", gid, exc)
                self._save()

            await asyncio.sleep(max(1, 60 - datetime.now().second))

    def _is_quiet_time(self, now=None):
        """按插件配置判断是否处于免打扰时段，支持跨午夜时间段。"""
        if not self._config_bool("enable_quiet_hours", True):
            return False
        now = now or datetime.now()
        try:
            start_text = str(self.config.get("quiet_start", "00:00")).strip()
            end_text = str(self.config.get("quiet_end", "08:00")).strip()
            start_h, start_m = (int(x) for x in start_text.split(":", 1))
            end_h, end_m = (int(x) for x in end_text.split(":", 1))
            if not (0 <= start_h <= 24 and 0 <= end_h <= 24 and 0 <= start_m <= 59 and 0 <= end_m <= 59):
                raise ValueError("时间必须是 HH:MM")
            if start_h == 24 and start_m != 0 or end_h == 24 and end_m != 0:
                raise ValueError("24 点只允许填写 24:00")
            if start_h == 24:
                start_h = 0
            if end_h == 24:
                end_h = 0
            current = now.hour * 60 + now.minute
            start = start_h * 60 + start_m
            end = end_h * 60 + end_m
            if start == end:
                return True
            if start < end:
                return start <= current < end
            return current >= start or current < end
        except Exception as exc:
            logger.warning("免打扰时间配置无效（需使用 HH:MM 格式），本次不启用免打扰判断：%s", exc)
            return False

    def _config_bool(self, key, default=True):
        """兼容 AstrBot 配置对象及普通 dict，安全读取布尔配置。"""
        try:
            value = self.config.get(key, default)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "开启", "开")
            return bool(value)
        except Exception:
            return default

    async def _random_group_member_for_lucky(self, gid):
        """为每日天选之子从真实群成员中抽选；不依赖玩家是否玩过游戏。"""
        bot = self._group_bots.get(str(gid))
        if bot is None:
            return None
        group_value = int(gid) if str(gid).isdigit() else gid
        api = getattr(bot, "api", None)
        callers = [getattr(api, "call_action", None) if api is not None else None,
                   getattr(bot, "call_action", None)]
        for caller in callers:
            if not callable(caller):
                continue
            for kwargs in ({"group_id": group_value, "no_cache": True}, {"group_id": group_value}):
                try:
                    result = caller("get_group_member_list", **kwargs)
                    if hasattr(result, "__await__"):
                        result = await asyncio.wait_for(result, timeout=8)
                    if isinstance(result, dict):
                        result = result.get("data", result.get("members", result.get("result")))
                    if not isinstance(result, (list, tuple)):
                        continue
                    candidates = []
                    for member in result:
                        if isinstance(member, dict):
                            uid = member.get("user_id", member.get("qq", member.get("id")))
                            name = member.get("card") or member.get("nickname") or member.get("name")
                        else:
                            uid = getattr(member, "user_id", getattr(member, "qq", getattr(member, "id", None)))
                            name = getattr(member, "card", None) or getattr(member, "nickname", None) or getattr(member, "name", None)
                        if uid is not None:
                            candidates.append((str(uid), str(name or f"群友{str(uid)[-4:]}")))
                    if candidates:
                        return random.choice(candidates)
                except Exception as exc:
                    logger.debug("获取群 %s 天选之子候选名单失败：%s", gid, exc)
        # 群成员 API 不可用时，退回到已有游戏参与者。
        users = self._group(gid).get("users", {})
        candidates = [(str(uid), str(obj.get("name") or f"群友{str(uid)[-4:]}"))
                      for uid, obj in users.items() if str(uid)]
        return random.choice(candidates) if candidates else None

    async def _hourly_random_flick(self, gid):
        """从平台返回的全体群成员中抽选一人；整点事件固定命中并播报。"""
        g = self._group(gid)
        # 管理面板是全局总开关；关闭时优先阻止所有群的整点播报。
        panel_enabled = self._config_bool("enable_hourly_broadcast", True)
        if not panel_enabled:
            logger.info("插件配置已关闭整点播报，跳过群 %s", gid)
            return
        if g.get("hourly_enabled", True) is False:
            logger.info("群 %s 已关闭整点播报，跳过", gid)
            return

        # sender_id 使用不会与正常 QQ 号冲突的哨兵值，因此不会刻意排除任何群成员。
        selected = await self._random_group_member_for_announcement(gid)
        if not selected:
            logger.warning("群 %s 获取不到全体群成员名单，无法保证全群随机抽选", gid)
            await self._send_group(gid, "⏰【整点随机弹射】本次未能获取群成员名单，播报未完成。请检查 QQ 适配器的群成员列表接口。")
            return

        target_id, target_name = selected
        lucky = g.get("daily_lucky", {})
        today = datetime.now().strftime("%Y-%m-%d")
        if lucky.get("date") == today and str(lucky.get("uid", "")) == str(target_id):
            await self._send_group(gid, f"✨【天选之子护体】本小时抽中了 {target_name}，但对方拥有今日免疫，攻击无效！")
            return
        target, before = self._add_booger(gid, target_id, target_name)
        self._record_hit(gid, target_id, target_name)
        message = (f"⏰🎯【整点随机弹射】本小时全群随机抽中了 {target_name}！必中成功！"
                   f"新增 1 颗鼻屎，现在库存 {target['count']} 颗，恶臭值 {target['stink']}/100。")
        self._save()
        await self._send_group(gid, message)

    async def _random_group_member_for_announcement(self, gid):
        """获取平台全体群成员；对接口调用设置超时，失败时交由整点流程发送提示。"""
        bot = self._group_bots.get(str(gid))
        if bot is None:
            logger.warning("群 %s 尚未缓存到最近的群消息 bot 对象", gid)
            return None

        group_value = int(gid) if str(gid).isdigit() else gid
        members = None
        errors = []
        api = getattr(bot, "api", None)
        api_call = getattr(api, "call_action", None) if api is not None else None
        bot_call = getattr(bot, "call_action", None)

        # aiocqhttp 常见签名是 call_action(action, **params)；兼容 no_cache 参数差异。
        for caller, label in ((api_call, "bot.api.call_action"), (bot_call, "bot.call_action")):
            if not callable(caller):
                continue
            for kwargs in ({"group_id": group_value, "no_cache": True}, {"group_id": group_value}):
                try:
                    result = caller("get_group_member_list", **kwargs)
                    if hasattr(result, "__await__"):
                        result = await asyncio.wait_for(result, timeout=8)
                    if isinstance(result, dict):
                        result = result.get("data", result.get("members", result.get("result")))
                    if isinstance(result, (list, tuple)) and result:
                        members = list(result)
                        logger.info("群 %s 通过 %s 获取到 %s 名群成员", gid, label, len(members))
                        break
                    errors.append(f"{label}: 返回类型/成员列表为空 ({type(result).__name__})")
                except asyncio.TimeoutError:
                    errors.append(f"{label}: 调用超时")
                except Exception as exc:
                    errors.append(f"{label}: {type(exc).__name__}: {exc}")
            if members:
                break

        if not members:
            logger.warning("群 %s 获取群成员列表失败：%s", gid, "；".join(errors) or "没有可用的 API")
            return None

        candidates = []
        for member in members:
            if isinstance(member, dict):
                mid = member.get("user_id", member.get("qq", member.get("id")))
                name = member.get("card") or member.get("nickname") or member.get("name")
            else:
                mid = getattr(member, "user_id", getattr(member, "qq", getattr(member, "id", None)))
                name = getattr(member, "card", None) or getattr(member, "nickname", None) or getattr(member, "name", None)
            if mid is not None:
                candidates.append((str(mid), str(name or f"群友{str(mid)[-4:]}")))

        if not candidates:
            logger.warning("群 %s 的成员 API 返回了数据，但未找到任何 user_id", gid)
            return None
        return random.choice(candidates)

    async def _send_group(self, gid, message, target_id=None):
        """用事件中记录的真实 UMO 主动发送；需要时插入真正的 At 消息段。"""
        gid = str(gid)
        umo = self._group_umos.get(gid) or self._group(gid).get("session_umo")
        if not umo:
            raise RuntimeError("尚未记录本群真实 unified_msg_origin；请先让群内产生一条消息后再等待整点任务")
        api = getattr(self.context, "send_message", None)
        if not callable(api):
            raise RuntimeError("当前 AstrBot Context 没有 send_message API")
        chain = MessageChain()
        if target_id is not None:
            # 先添加真正的 At 组件，再添加正文，不能只把昵称写进纯文本。
            try:
                chain.chain.append(At(qq=int(target_id) if str(target_id).isdigit() else str(target_id)))
            except Exception:
                chain.chain.append(At(qq=str(target_id)))
            chain.chain.append(Plain(text=" "))
        chain.message(str(message))
        result = api(umo, chain)
        if hasattr(result, "__await__"):
            await result

    async def initialize(self):
        try:
            self._scheduler_task = asyncio.create_task(self._announcement_loop())
            logger.info("群友弹鼻屎定时任务已启动：每小时整点随机弹射，每天 23:00 公布鼻屎王")
        except Exception as exc:
            self._scheduler_task = None
            logger.exception("群友弹鼻屎定时任务启动失败：%s", exc)

    async def terminate(self):
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
