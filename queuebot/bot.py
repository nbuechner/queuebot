from __future__ import annotations
import json, os, re, requests, asyncio, pytz, importlib, traceback, logging
from aiohttp.web import Request, Response, json_response
from datetime import datetime, timedelta
from launchpadlib.launchpad import Launchpad
from maubot import Plugin, MessageEvent
from maubot.handlers import command
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from mautrix.util import background_task
from mautrix.types import (
    EventType,
    MemberStateEventContent,
    PowerLevelStateEventContent,
    RoomID,
    RoomAlias,
    StateEvent,
    UserID,
)
from pathlib import Path
from time import time
from typing import Type, Tuple
from urllib.parse import urlparse, unquote
from .plugs import queue, tracker, packageset
from .floodprotection import FloodProtection

qbot_change_level = EventType.find("com.ubuntu.qbot", t_class=EventType.Class.STATE)

class Config(BaseProxyConfig):
  def do_update(self, helper: ConfigUpdateHelper) -> None:
    helper.copy("whitelist")
    helper.copy("rooms")

class Queuebot(Plugin):
  reminder_loop_task: asyncio.Future
  VERBOSE=False
  plugin_queue_new = queue.Queue("New", VERBOSE)
  plugin_queue_unapproved = queue.Queue("Unapproved", VERBOSE)
  plugin_packageset = packageset.Packageset("Packageset", VERBOSE)
  plugin_tracker = tracker.Tracker("Builds", VERBOSE)
  room_ids = []
  room_mapping = {}
  power_level_cache: dict[RoomID, tuple[int, PowerLevelStateEventContent]]

  async def start(self) -> None:
    self.config.load_and_update()
    self.flood_protection = FloodProtection()
    self.power_level_cache = {}
    logger = logging.getLogger(self.id)
    logger.setLevel(logging.DEBUG)
    self.log = logger
    if await self.resolve_room_aliases():
        self.poll_task = asyncio.create_task(self.poll_plugins())
        self.log.info("Queuebot started")

  async def stop(self) -> None:
    await super().stop()
    self.poll_task.cancel()

  async def get_power_levels(self, room_id: RoomID) -> PowerLevelStateEventContent:
        try:
            expiry, levels = self.power_level_cache[room_id]
            if expiry < int(time()):
                return levels
        except KeyError:
            pass
        levels = await self.client.get_state_event(room_id, EventType.ROOM_POWER_LEVELS)
        now = int(time())
        self.power_level_cache[room_id] = (now + 5 * 60, levels)
        return levels

  async def can_manage(self, evt: MessageEvent) -> bool:
        if evt.sender in self.config["whitelist"]:
            return True
        levels = await self.get_power_levels(evt.room_id)
        user_level = levels.get_user_level(evt.sender)
        state_level = levels.get_event_level(qbot_change_level)
        if not isinstance(state_level, int):
            state_level = 50
        if user_level < state_level:
            return False
        return True

  @command.new(name="qbot", require_subcommand=False)
  async def qbot(self, evt: MessageEvent) -> None:
        if not await self.can_manage(evt) and self.flood_protection.flood_check(evt.sender):
            await evt.respond("You don't have the permission to use this command.")
            return False
        await evt.respond("Invalid argument. Example: !qbot mute queue")
        return False
  @qbot.subcommand("mute", aliases=["unmute"])
  @command.argument("plugin", "(un)mute a plugin", required=False)
  async def mute(self, evt: MessageEvent, plugin: str) -> None:
    if not await self.can_manage(evt) and self.flood_protection.flood_check(evt.sender):
        await evt.respond("You don't have the permission to manage mutes.")
        return False
    if not plugin or plugin not in ["queue", "tracker", "packageset"]:
        await evt.respond("Invalid plugin. Valid plugins are queue, tracker and packageset. Example: !qbot mute queue")
        return False
    room_alias = self.room_mapping[evt.room_id]
    if plugin in self.config["rooms"][room_alias]['mute']:
        self.config["rooms"][room_alias]['mute'].remove(plugin)
        await evt.respond(f"Unmuted {plugin}")
    else:
        self.config["rooms"][room_alias]['mute'].append(plugin)
        await evt.respond(f"Muted {plugin}")
    self.config.save()

  async def resolve_room_alias_to_id(self, room_alias: str):
        try:
            room_id = await self.client.resolve_room_alias(RoomAlias(room_alias))
            return room_id
        except Exception as e:
            self.log.error(f"Error resolving room alias {room_alias}: {e}")
            self.log.debug(traceback.format_exc())
            return None

  async def resolve_room_aliases(self):
    self.room_ids = []
    self.room_mapping = {}
    for room_alias in self.config["rooms"]:
        if room_alias.startswith("#"):
            if room_id_obj := await self.resolve_room_alias_to_id(room_alias):
                room_id = str(room_id_obj.room_id)
                self.room_ids.append(room_id)
                self.room_mapping[room_id] = room_alias
                self.log.info("Added room " + room_alias + " with id " + room_id)
        elif room_alias.startswith("!"):
            self.room_ids.append(room_alias)
            self.room_mapping[room_alias] = room_alias
            self.log.info("Added room id " + room_alias)
        else:
            self.log.debug("Error addming room " + room_alias)
    return True
    
  def check_access_sender(self, sender):
      if sender in self.config["whitelist"]:
         return True
      return False

  def check_plugin_filter_mute(self, plugin_name, queue, notice, room_id, room_alias):
    try:
        # is the plugin enabled
        if self.config['rooms'][room_alias].get(plugin_name) is None:
            self.log.debug(f"plugin_name {plugin_name} is None for {room_alias}")
            return False
        queues = self.config['rooms'][room_alias].get(plugin_name)
        if isinstance(queues, list):
            if queue not in queues:
                self.log.debug(f"queue {plugin_name}.{queue} not queues for {room_alias}")
                return False
        elif isinstance(queues, str):
            if queue != queues:
                self.log.debug(f"queue {plugin_name}.{queue} not in queue string for {room_alias}")
                return False
        else:
            self.log.debug(f"queues is not str or list for {room_alias}")
            return False
        
        if self.config['rooms'][room_alias].get('mute') is None:
            return True
        mutes = self.config['rooms'][room_alias].get('mute')
        mute_name = f"{plugin_name}.{queue}".lower()
        if not isinstance(mutes, list) or len(mutes) < 1:
            self.log.debug(f"mutes in {room_alias} is not a valid list or empty")
        elif mute_name in mutes:
            self.log.debug(f"{mute_name} is in mutes for {room_alias}")
            return False
        # is there a filter
        filter_name = plugin_name + '_filter'.lower()
        if self.config['rooms'][room_alias].get(filter_name) is not None:
            if str(self.config['rooms'][room_alias][filter_name]).lower() not in notice.lower():
                self.log.debug(f"not sending notice to {room_alias} as it matches filter " + str(self.config['rooms'][room_alias][filter_name]).lower() )
                return False
    except Exception as e:
        self.log.debug("Error checking filter or mute: " + str(e))
        self.log.debug(traceback.format_exc())
        return False
    
    return True

  async def poll_plugins(self) -> None:
        try:
            await self._poll_plugins()
        except asyncio.CancelledError:
            self.log.info("Polling stopped")
        except Exception:
            self.log.exception("Fatal error while polling plugins")
  async def _poll_plugins(self) -> None:
        self.log.info("Polling started")
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                self.log.info("Polling stopped")
            except Exception:
                self.log.exception("Error while polling plugins")
            self.log.debug("Sleeping " + str(self.config["update_interval"] * 60) + " seconds")
            await asyncio.sleep(self.config["update_interval"] * 60)
  async def _poll_once(self) -> None:
    try:
        plugins_to_poll = [self.plugin_queue_new, self.plugin_queue_unapproved, self.plugin_tracker, self.plugin_packageset]
        for plugin_name in plugins_to_poll:
            if not hasattr(plugin_name, 'update'):
                continue
            notices = plugin_name.update()
            self.log.debug('update() function called on ' + str(plugin_name.name) + '.' + str(plugin_name.queue))
            sent_count = 0
            if notices:
                    self.log.debug(f"New notices available")
                    if await self.resolve_room_aliases():
                        for notice in notices:
                            for room_id in self.room_ids:
                                self.log.debug(f"Checking notices or {room_id}")
                                try:
                                    room_alias = self.room_mapping[room_id]
                                    self.log.debug(f"Checking notices or {room_alias}  ( {room_id} )")
                                    if self.check_plugin_filter_mute(plugin_name=plugin_name.name,queue=plugin_name.queue, notice=notice[0], room_id=room_id, room_alias=room_alias):
                                        self.log.debug(f"new notice from {plugin_name.name}.{plugin_name.queue} to {room_alias}")
                                        self.log.debug(f"sent count: {sent_count}")
                                        if sent_count >= 5:
                                            self.log.debug(f"sent count reached: {sent_count} sleeping 60 secs")
                                            if await asyncio.sleep(60):
                                                sent_count = 0
                                                await self.client.send_notice(room_id, notice[0])
                                        else:
                                            await self.client.send_notice(room_id, notice[0])
                                        sent_count += 1
                                except Exception as e:
                                    self.log.debug(f"Error sending notice to {room_id}: {e}")
                                    self.log.debug(traceback.format_exc())
                                    pass
    except Exception as e:
        self.log.debug(f"Error polling plugins: {e}")
        self.log.debug(traceback.format_exc())
        pass

  @classmethod
  def get_config_class(cls) -> Type[BaseProxyConfig]:
    return Config
