# -*- coding: utf-8 -*-
#
# gPodder - A media aggregator and podcast client
# Copyright (c) 2005-2018 The gPodder Team
# Copyright (c) 2011 Neal H. Walfield
#
# gPodder is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# gPodder is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#


#
#  gpodder.model - Core model classes for gPodder (2009-08-13)
#  Based on libpodcasts.py (thp, 2005-10-29)
#

import collections
import datetime
import glob
import hashlib
import logging
import os
import re
import shutil
import string
import time

import gpodder
import podcastparser
from gpodder import (coverart, escapist_videos, feedcore, schema, util, vimeo,
                     youtube)
from gpodder.jsonconfig import JsonConfig

logger = logging.getLogger(__name__)

_ = gpodder.gettext


class CustomFeed(feedcore.ExceptionWithData): pass


class gPodderFetcher(feedcore.Fetcher):
    """
    This class extends the feedcore Fetcher with the gPodder User-Agent and the
    Proxy handler based on the current settings in gPodder.
    """
    custom_handlers = []

    def fetch_channel(self, channel):
        for handler in self.custom_handlers:
            custom_feed = handler.fetch_channel(channel)
            if custom_feed is not None:
                return feedcore.Result(feedcore.CUSTOM_FEED, custom_feed)
        # If we have a username or password, rebuild the url with them included
        # Note: using a HTTPBasicAuthHandler would be pain because we need to
        # know the realm. It can be done, but I think this method works, too
        url = channel.authenticate_url(channel.url)
        return self.fetch(url, channel.http_etag, channel.http_last_modified)

    def _resolve_url(self, url):
        url = youtube.get_real_channel_url(url)
        url = vimeo.get_real_channel_url(url)
        url = escapist_videos.get_real_channel_url(url)
        return url

    @classmethod
    def register(cls, handler):
        cls.custom_handlers.append(handler)

    @classmethod
    def unregister(cls, handler):
        cls.custom_handlers.remove(handler)


# The "register" method is exposed here for external usage
register_custom_handler = gPodderFetcher.register

# The "unregister" method is exposed here for external usage
unregister_custom_handler = gPodderFetcher.unregister

# Our podcast model:
#
# database -> podcast -> episode -> download/playback
#  podcast.parent == db
#  podcast.children == [episode, ...]
#  episode.parent == podcast
#
# - normally: episode.children = (None, None)
# - downloading: episode.children = (DownloadTask(), None)
# - playback: episode.children = (None, PlaybackTask())


class PodcastModelObject(object):
    """
    A generic base class for our podcast model providing common helper
    and utility functions.
    """
    __slots__ = ('id', 'parent', 'children', '_parsed_ext_data', '_ext_data_dirty')

    def __init__(self):
        self._parsed_ext_data = None
        self._ext_data_dirty = False

    @classmethod
    def create_from_dict(cls, d, *args):
        """
        Create a new object, passing "args" to the constructor
        and then updating the object with the values from "d".
        """
        o = cls(*args)

        # XXX: all(map(lambda k: hasattr(o, k), d))?
        for k, v in d.items():
            setattr(o, k, v)

        return o

    def get_ext_data(self, extension):
        """
        Get extension-specific storage for this object.
        It's a JsonConfigSubtree so setting attribute values will be saved.
        Make sure to use a versionned key.
        """
        if not self._parsed_ext_data:
            self._parsed_ext_data = JsonConfig(self.ext_data, on_key_changed=self._mark_ext_data_dirty)
        return getattr(self._parsed_ext_data, extension)

    def _mark_ext_data_dirty(self, name, old_value, new_value):
        self._ext_data_dirty = True

    def save_ext_data(self):
        if self._ext_data_dirty:
            self.ext_data = repr(self._parsed_ext_data)
            self._ext_data_dirty = False


class PodcastEpisode(PodcastModelObject):
    """holds data for one object in a channel"""
    # In theory, Linux can have 255 bytes (not characters!) in a filename, but
    # filesystems like eCryptFS store metadata in the filename, making the
    # effective number of characters less than that. eCryptFS recommends
    # 140 chars, we use 120 here (140 - len(extension) - len(".partial")).
    # References: gPodder bug 1898, http://unix.stackexchange.com/a/32834
    MAX_FILENAME_LENGTH = 120  # without extension
    MAX_FILENAME_WITH_EXT_LENGTH = 140 - len(".partial")  # with extension

    __slots__ = schema.EpisodeColumns

    def _deprecated(self):
        raise Exception('Property is deprecated!')

    is_played = property(fget=_deprecated, fset=_deprecated)
    is_locked = property(fget=_deprecated, fset=_deprecated)

    def has_website_link(self):
        return bool(self.link) and (self.link != self.url or
                youtube.is_video_link(self.link))

    @classmethod
    def from_podcastparser_entry(cls, entry, channel):
        episode = cls(channel)
        episode.guid = entry['guid']
        episode.title = entry['title']
        episode.link = entry['link']
        episode.description = entry['description']
        if entry.get('description_html'):
            episode.description_html = entry['description_html']

        episode.total_time = entry['total_time']
        episode.published = entry['published']
        episode.payment_url = entry['payment_url']

        audio_available = any(enclosure['mime_type'].startswith('audio/') for enclosure in entry['enclosures'])
        video_available = any(enclosure['mime_type'].startswith('video/') for enclosure in entry['enclosures'])

        for enclosure in entry['enclosures']:
            episode.mime_type = enclosure['mime_type']

            # Skip images in feeds if audio or video is available (bug 979)
            # This must (and does) also look in Media RSS enclosures (bug 1430)
            if episode.mime_type.startswith('image/') and (audio_available or video_available):
                continue

            # If we have audio or video available later on, skip
            # 'application/octet-stream' data types (fixes Linux Outlaws)
            if episode.mime_type == 'application/octet-stream' and (audio_available or video_available):
                continue

            episode.url = util.normalize_feed_url(enclosure['url'])
            if not episode.url:
                continue

            episode.file_size = enclosure['file_size']
            return episode

        # Brute-force detection of the episode link
        episode.url = util.normalize_feed_url(entry['link'])
        if not episode.url:
            return None

        if any(mod.is_video_link(episode.url) for mod in (youtube, vimeo, escapist_videos)):
            return episode

        # Check if we can resolve this link to a audio/video file
        filename, extension = util.filename_from_url(episode.url)
        file_type = util.file_type_by_extension(extension)

        # The link points to a audio or video file - use it!
        if file_type is not None:
            return episode

        return None

    def __init__(self, channel):
        super().__init__()
        self.parent = channel
        self.podcast_id = self.parent.id
        self.children = (None, None)

        self.id = None
        self.url = ''
        self.title = ''
        self.file_size = 0
        self.mime_type = 'application/octet-stream'
        self.guid = ''
        self.description = ''
        self.description_html = ''
        self.link = ''
        self.published = 0
        self.download_filename = None
        self.payment_url = None
        self.ext_data = None

        self.state = gpodder.STATE_NORMAL
        self.is_new = True
        self.archive = channel.auto_archive_episodes

        # Time attributes
        self.total_time = 0
        self.current_position = 0
        self.current_position_updated = 0

        # Timestamp of last playback time
        self.last_playback = 0

    @property
    def channel(self):
        return self.parent

    @property
    def db(self):
        return self.parent.parent.db

    @property
    def trimmed_title(self):
        """Return the title with the common prefix trimmed"""
        # Minimum amount of leftover characters after trimming. This
        # avoids things like "Common prefix 123" to become just "123".
        # If there are LEFTOVER_MIN or less characters after trimming,
        # the original title will be returned without trimming.
        LEFTOVER_MIN = 5

        # "Podcast Name - Title" and "Podcast Name: Title" -> "Title"
        for postfix in (' - ', ': '):
            prefix = self.parent.title + postfix
            if (self.title.startswith(prefix) and
                    len(self.title) - len(prefix) > LEFTOVER_MIN):
                return self.title[len(prefix):]

        regex_patterns = [
            # "Podcast Name <number>: ..." -> "<number>: ..."
            r'^%s (\d+: .*)' % re.escape(self.parent.title),

            # "Episode <number>: ..." -> "<number>: ..."
            r'Episode (\d+:.*)',
        ]

        for pattern in regex_patterns:
            if re.match(pattern, self.title):
                title = re.sub(pattern, r'\1', self.title)
                if len(title) > LEFTOVER_MIN:
                    return title

        # "#001: Title" -> "001: Title"
        if (
                not self.parent._common_prefix and
                re.match('^#\d+: ', self.title) and
                len(self.title) - 1 > LEFTOVER_MIN):
            return self.title[1:]

        if (self.parent._common_prefix is not None and
                self.title.startswith(self.parent._common_prefix) and
                len(self.title) - len(self.parent._common_prefix) > LEFTOVER_MIN):
            return self.title[len(self.parent._common_prefix):]

        return self.title

    def _set_download_task(self, download_task):
        self.children = (download_task, self.children[1])

    def _get_download_task(self):
        return self.children[0]

    download_task = property(_get_download_task, _set_download_task)

    @property
    def downloading(self):
        task = self.download_task
        if task is None:
            return False

        return task.status in (task.DOWNLOADING, task.QUEUED, task.PAUSED)

    def check_is_new(self):
        return (self.state == gpodder.STATE_NORMAL and self.is_new and
                not self.downloading)

    def save(self):
        gpodder.user_extensions.on_episode_save(self)
        self.save_ext_data()
        self.db.save_episode(self)

    def on_downloaded(self, filename):
        self.state = gpodder.STATE_DOWNLOADED
        self.is_new = True
        self.file_size = os.path.getsize(filename)
        self.save()

    def set_state(self, state):
        self.state = state
        self.save()

    def playback_mark(self):
        self.is_new = False
        self.last_playback = int(time.time())
        gpodder.user_extensions.on_episode_playback(self)
        self.save()

    def mark(self, state=None, is_played=None, is_locked=None):
        if state is not None:
            self.state = state
        if is_played is not None:
            self.is_new = not is_played

            # "Mark as new" must "undelete" the episode
            if self.is_new and self.state == gpodder.STATE_DELETED:
                self.state = gpodder.STATE_NORMAL
        if is_locked is not None:
            self.archive = is_locked
        self.save()

    def age_in_days(self):
        return util.file_age_in_days(self.local_filename(create=False,
                check_only=True))

    age_int_prop = property(fget=age_in_days)

    def get_age_string(self):
        return util.file_age_to_string(self.age_in_days())

    age_prop = property(fget=get_age_string)

    def one_line_description(self):
        MAX_LINE_LENGTH = 120
        desc = util.remove_html_tags(self.description or '')
        desc = re.sub('\s+', ' ', desc).strip()
        if not desc:
            return _('No description available')
        else:
            # Decode the description to avoid gPodder bug 1277
            desc = util.convert_bytes(desc).strip()

            if len(desc) > MAX_LINE_LENGTH:
                return desc[:MAX_LINE_LENGTH] + '...'
            else:
                return desc

    def delete_from_disk(self):
        filename = self.local_filename(create=False, check_only=True)
        if filename is not None:
            gpodder.user_extensions.on_episode_delete(self, filename)
            util.delete_file(filename)

        self.set_state(gpodder.STATE_DELETED)

    def get_playback_url(self, fmt_ids=None, vimeo_fmt=None, allow_partial=False):
        """Local (or remote) playback/streaming filename/URL

        Returns either the local filename or a streaming URL that
        can be used to playback this episode.

        Also returns the filename of a partially downloaded file
        in case partial (preview) playback is desired.
        """
        url = self.local_filename(create=False)

        if (allow_partial and url is not None and
                os.path.exists(url + '.partial')):
            return url + '.partial'

        if url is None or not os.path.exists(url):
            url = self.url
            url = youtube.get_real_download_url(url, fmt_ids)
            url = vimeo.get_real_download_url(url, vimeo_fmt)
            url = escapist_videos.get_real_download_url(url)

        return url

    def find_unique_file_name(self, filename, extension):
        # Remove leading and trailing whitespace + dots (to avoid hidden files)
        filename = filename.strip('.' + string.whitespace) + extension

        for name in util.generate_names(filename):
            if (not self.db.episode_filename_exists(self.podcast_id, name) or
                    self.download_filename == name):
                return name

    def local_filename(self, create, force_update=False, check_only=False,
            template=None, return_wanted_filename=False):
        """Get (and possibly generate) the local saving filename

        Pass create=True if you want this function to generate a
        new filename if none exists. You only want to do this when
        planning to create/download the file after calling this function.

        Normally, you should pass create=False. This will only
        create a filename when the file already exists from a previous
        version of gPodder (where we used md5 filenames). If the file
        does not exist (and the filename also does not exist), this
        function will return None.

        If you pass force_update=True to this function, it will try to
        find a new (better) filename and move the current file if this
        is the case. This is useful if (during the download) you get
        more information about the file, e.g. the mimetype and you want
        to include this information in the file name generation process.

        If check_only=True is passed to this function, it will never try
        to rename the file, even if would be a good idea. Use this if you
        only want to check if a file exists.

        If "template" is specified, it should be a filename that is to
        be used as a template for generating the "real" filename.

        The generated filename is stored in the database for future access.

        If return_wanted_filename is True, the filename will not be written to
        the database, but simply returned by this function (for use by the
        "import external downloads" feature).
        """
        if self.download_filename is None and (check_only or not create):
            return None

        ext = self.extension(may_call_local_filename=False)

        if not check_only and (force_update or not self.download_filename):
            # Avoid and catch gPodder bug 1440 and similar situations
            if template == '':
                logger.warn('Empty template. Report this podcast URL %s',
                        self.channel.url)
                template = None

            # Try to find a new filename for the current file
            if template is not None:
                # If template is specified, trust the template's extension
                episode_filename, ext = os.path.splitext(template)
            else:
                episode_filename, _ = util.filename_from_url(self.url)

            if 'redirect' in episode_filename and template is None:
                # This looks like a redirection URL - force URL resolving!
                logger.warn('Looks like a redirection to me: %s', self.url)
                url = util.get_real_url(self.channel.authenticate_url(self.url))
                logger.info('Redirection resolved to: %s', url)
                episode_filename, _ = util.filename_from_url(url)

            # Use title for YouTube, Vimeo and Soundcloud downloads
            if (youtube.is_video_link(self.url) or
                    vimeo.is_video_link(self.url) or
                    escapist_videos.is_video_link(self.url) or
                    episode_filename == 'stream'):
                episode_filename = self.title

            # If the basename is empty, use the md5 hexdigest of the URL
            if not episode_filename or episode_filename.startswith('redirect.'):
                logger.error('Report this feed: Podcast %s, episode %s',
                        self.channel.url, self.url)
                episode_filename = hashlib.md5(self.url.encode('utf-8')).hexdigest()

            # Also sanitize ext (see #591 where ext=.mp3?dest-id=754182)
            fn_template, ext = util.sanitize_filename_ext(
                episode_filename,
                ext,
                self.MAX_FILENAME_LENGTH,
                self.MAX_FILENAME_WITH_EXT_LENGTH)
            ext = '.' + ext
            # Find a unique filename for this episode
            wanted_filename = self.find_unique_file_name(fn_template, ext)

            if return_wanted_filename:
                # return the calculated filename without updating the database
                return wanted_filename

            # The old file exists, but we have decided to want a different filename
            if self.download_filename and wanted_filename != self.download_filename:
                # there might be an old download folder crawling around - move it!
                new_file_name = os.path.join(self.channel.save_dir, wanted_filename)
                old_file_name = os.path.join(self.channel.save_dir, self.download_filename)
                if os.path.exists(old_file_name) and not os.path.exists(new_file_name):
                    logger.info('Renaming %s => %s', old_file_name, new_file_name)
                    os.rename(old_file_name, new_file_name)
                elif force_update and not os.path.exists(old_file_name):
                    # When we call force_update, the file might not yet exist when we
                    # call it from the downloading code before saving the file
                    logger.info('Choosing new filename: %s', new_file_name)
                else:
                    logger.warn('%s exists or %s does not', new_file_name, old_file_name)
                logger.info('Updating filename of %s to "%s".', self.url, wanted_filename)
            elif self.download_filename is None:
                logger.info('Setting download filename: %s', wanted_filename)
            self.download_filename = wanted_filename
            self.save()

        if return_wanted_filename:
            # return the filename, not full path
            return self.download_filename
        return os.path.join(self.channel.save_dir, self.download_filename)

    def extension(self, may_call_local_filename=True):
        filename, ext = util.filename_from_url(self.url)
        if may_call_local_filename:
            filename = self.local_filename(create=False)
            if filename is not None:
                filename, ext = os.path.splitext(filename)
        # if we can't detect the extension from the url fallback on the mimetype
        if ext == '' or util.file_type_by_extension(ext) is None:
            ext = util.extension_from_mimetype(self.mime_type)
        return ext

    def mark_new(self):
        self.is_new = True
        self.save()

    def mark_old(self):
        self.is_new = False
        self.save()

    def file_exists(self):
        filename = self.local_filename(create=False, check_only=True)
        if filename is None:
            return False
        else:
            return os.path.exists(filename)

    def was_downloaded(self, and_exists=False):
        if self.state != gpodder.STATE_DOWNLOADED:
            return False
        if and_exists and not self.file_exists():
            return False
        return True

    def sync_filename(self, use_custom=False, custom_format=None):
        if use_custom:
            return util.object_string_formatter(custom_format,
                    episode=self, podcast=self.channel)
        else:
            return self.title

    def file_type(self):
        # Assume all YouTube/Vimeo links are video files
        if youtube.is_video_link(self.url) or vimeo.is_video_link(self.url) or escapist_videos.is_video_link(self.url):
            return 'video'

        return util.file_type_by_extension(self.extension())

    @property
    def basename(self):
        return os.path.splitext(os.path.basename(self.url))[0]

    @property
    def pubtime(self):
        """
        Returns published time as HHMM (or 0000 if not available)
        """
        try:
            return datetime.datetime.fromtimestamp(self.published).strftime('%H%M')
        except:
            logger.warn('Cannot format pubtime: %s', self.title, exc_info=True)
            return '0000'

    def playlist_title(self):
        """Return a title for this episode in a playlist

        The title will be composed of the podcast name, the
        episode name and the publication date. The return
        value is the canonical representation of this episode
        in playlists (for example, M3U playlists).
        """
        return '%s - %s (%s)' % (self.channel.title,
                self.title,
                self.cute_pubdate())

    def cute_pubdate(self):
        result = util.format_date(self.published)
        if result is None:
            return '(%s)' % _('unknown')
        else:
            return result

    pubdate_prop = property(fget=cute_pubdate)

    def published_datetime(self):
        return datetime.datetime.fromtimestamp(self.published)

    @property
    def sortdate(self):
        return self.published_datetime().strftime('%Y-%m-%d')

    @property
    def pubdate_day(self):
        return self.published_datetime().strftime('%d')

    @property
    def pubdate_month(self):
        return self.published_datetime().strftime('%m')

    @property
    def pubdate_year(self):
        return self.published_datetime().strftime('%y')

    def is_finished(self):
        """Return True if this episode is considered "finished playing"

        An episode is considered "finished" when there is a
        current position mark on the track, and when the
        current position is greater than 99 percent of the
        total time or inside the last 10 seconds of a track.
        """
        return (self.current_position > 0 and self.total_time > 0 and
                (self.current_position + 10 >= self.total_time or
                 self.current_position >= self.total_time * .99))

    def get_play_info_string(self, duration_only=False):
        duration = util.format_time(self.total_time)
        if duration_only and self.total_time > 0:
            return duration
        elif self.is_finished():
            return '%s (%s)' % (_('Finished'), duration)
        elif self.current_position > 0 and \
                self.current_position != self.total_time:
            position = util.format_time(self.current_position)
            return '%s / %s' % (position, duration)
        elif self.total_time > 0:
            return duration
        else:
            return '-'

    def update_from(self, episode):
        for k in ('title', 'url', 'description', 'description_html', 'link', 'published', 'guid', 'file_size', 'payment_url'):
            setattr(self, k, getattr(episode, k))


class PodcastChannel(PodcastModelObject):
    __slots__ = schema.PodcastColumns + ('_common_prefix',)

    UNICODE_TRANSLATE = {ord('ö'): 'o', ord('ä'): 'a', ord('ü'): 'u'}

    # Enumerations for download strategy
    STRATEGY_DEFAULT, STRATEGY_LATEST = list(range(2))

    # Description and ordering of strategies
    STRATEGIES = [
        (STRATEGY_DEFAULT, _('Default')),
        (STRATEGY_LATEST, _('Only keep latest')),
    ]

    MAX_FOLDERNAME_LENGTH = 60
    SECONDS_PER_DAY = 24 * 60 * 60
    SECONDS_PER_WEEK = 7 * 24 * 60 * 60
    EpisodeClass = PodcastEpisode

    feed_fetcher = gPodderFetcher()

    def __init__(self, model, id=None):
        super().__init__()
        self.parent = model
        self.children = []

        self.id = id
        self.url = None
        self.title = ''
        self.link = ''
        self.description = ''
        self.cover_url = None
        self.payment_url = None

        self.auth_username = ''
        self.auth_password = ''

        self.http_last_modified = None
        self.http_etag = None

        self.auto_archive_episodes = False
        self.download_folder = None
        self.pause_subscription = False
        self.sync_to_mp3_player = True
        self.cover_thumb = None

        self.section = _('Other')
        self._common_prefix = None
        self.download_strategy = PodcastChannel.STRATEGY_DEFAULT

        self.ext_data = None

        if self.id:
            self.children = self.db.load_episodes(self, self.episode_factory)
            self._determine_common_prefix()

    @property
    def model(self):
        return self.parent

    @property
    def db(self):
        return self.parent.db

    def get_download_strategies(self):
        for value, caption in PodcastChannel.STRATEGIES:
            yield self.download_strategy == value, value, caption

    def set_download_strategy(self, download_strategy):
        if download_strategy == self.download_strategy:
            return

        caption = dict(self.STRATEGIES).get(download_strategy)
        if caption is not None:
            logger.debug('Strategy for %s changed to %s', self.title, caption)
            self.download_strategy = download_strategy
        else:
            logger.warn('Cannot set strategy to %d', download_strategy)

    def rewrite_url(self, new_url):
        new_url = util.normalize_feed_url(new_url)
        if new_url is None:
            return None

        self.url = new_url
        self.http_etag = None
        self.http_last_modified = None
        self.save()
        return new_url

    def check_download_folder(self):
        """Check the download folder for externally-downloaded files

        This will try to assign downloaded files with episodes in the
        database.

        This will also cause missing files to be marked as deleted.
        """
        known_files = set()

        for episode in self.get_episodes(gpodder.STATE_DOWNLOADED):
            if episode.was_downloaded():
                filename = episode.local_filename(create=False)
                if filename is None:
                    # No filename has been determined for this episode
                    continue

                if not os.path.exists(filename):
                    # File has been deleted by the user - simulate a
                    # delete event (also marks the episode as deleted)
                    logger.debug('Episode deleted: %s', filename)
                    episode.delete_from_disk()
                    continue

                known_files.add(filename)

        existing_files = set(filename for filename in
                glob.glob(os.path.join(self.save_dir, '*'))
                if not filename.endswith('.partial'))

        ignore_files = ['folder' + ext for ext in
                coverart.CoverDownloader.EXTENSIONS]

        external_files = existing_files.difference(list(known_files) +
                [os.path.join(self.save_dir, ignore_file)
                 for ignore_file in ignore_files])
        if not external_files:
            return

        all_episodes = self.get_all_episodes()

        for filename in external_files:
            found = False

            basename = os.path.basename(filename)
            existing = [e for e in all_episodes if e.download_filename == basename]
            if existing:
                existing = existing[0]
                logger.info('Importing external download: %s', filename)
                existing.on_downloaded(filename)
                continue

            for episode in all_episodes:
                wanted_filename = episode.local_filename(create=True,
                        return_wanted_filename=True)
                if basename == wanted_filename:
                    logger.info('Importing external download: %s', filename)
                    episode.download_filename = basename
                    episode.on_downloaded(filename)
                    found = True
                    break

                wanted_base, wanted_ext = os.path.splitext(wanted_filename)
                target_base, target_ext = os.path.splitext(basename)
                if wanted_base == target_base:
                    # Filenames only differ by the extension
                    wanted_type = util.file_type_by_extension(wanted_ext)
                    target_type = util.file_type_by_extension(target_ext)

                    # If wanted type is None, assume that we don't know
                    # the right extension before the download (e.g. YouTube)
                    # if the wanted type is the same as the target type,
                    # assume that it's the correct file
                    if wanted_type is None or wanted_type == target_type:
                        logger.info('Importing external download: %s', filename)
                        episode.download_filename = basename
                        episode.on_downloaded(filename)
                        found = True
                        break

            if not found and not util.is_system_file(filename):
                logger.warn('Unknown external file: %s', filename)

    @classmethod
    def sort_key(cls, podcast):
        key = util.convert_bytes(podcast.title.lower())
        return re.sub('^the ', '', key).translate(cls.UNICODE_TRANSLATE)

    @classmethod
    def load(cls, model, url, create=True, authentication_tokens=None, max_episodes=0):
        existing = [p for p in model.get_podcasts() if p.url == url]

        if existing:
            return existing[0]

        if create:
            tmp = cls(model)
            tmp.url = url
            if authentication_tokens is not None:
                tmp.auth_username = authentication_tokens[0]
                tmp.auth_password = authentication_tokens[1]

            # Save podcast, so it gets an ID assigned before
            # updating the feed and adding saving episodes
            tmp.save()

            try:
                tmp.update(max_episodes)
            except Exception as e:
                logger.debug('Fetch failed. Removing buggy feed.')
                tmp.remove_downloaded()
                tmp.delete()
                raise

            # Determine the section in which this podcast should appear
            tmp.section = tmp._get_content_type()

            # Determine a new download folder now that we have the title
            tmp.get_save_dir(force_new=True)

            # Mark episodes as downloaded if files already exist (bug 902)
            tmp.check_download_folder()

            # Determine common prefix of episode titles
            tmp._determine_common_prefix()

            tmp.save()

            gpodder.user_extensions.on_podcast_subscribe(tmp)

            return tmp

    def episode_factory(self, d):
        """
        This function takes a dictionary containing key-value pairs for
        episodes and returns a new PodcastEpisode object that is connected
        to this object.

        Returns: A new PodcastEpisode object
        """
        return self.EpisodeClass.create_from_dict(d, self)

    def _consume_updated_title(self, new_title):
        # Replace multi-space and newlines with single space (Maemo bug 11173)
        new_title = re.sub('\s+', ' ', new_title).strip()

        # Only update the podcast-supplied title when we
        # don't yet have a title, or if the title is the
        # feed URL (e.g. we didn't find a title before).
        if not self.title or self.title == self.url:
            self.title = new_title

            # Start YouTube- and Vimeo-specific title FIX
            YOUTUBE_PREFIX = 'Uploads by '
            VIMEO_PREFIX = 'Vimeo / '
            if self.title.startswith(YOUTUBE_PREFIX):
                self.title = self.title[len(YOUTUBE_PREFIX):] + ' on YouTube'
            elif self.title.startswith(VIMEO_PREFIX):
                self.title = self.title[len(VIMEO_PREFIX):] + ' on Vimeo'
            # End YouTube- and Vimeo-specific title FIX

    def _consume_metadata(self, title, link, description, cover_url,
            payment_url):
        self._consume_updated_title(title)
        self.link = link
        self.description = description
        self.cover_url = cover_url
        self.payment_url = payment_url
        self.save()

    def _consume_custom_feed(self, custom_feed, max_episodes=0):
        self._consume_metadata(custom_feed.get_title(),
                custom_feed.get_link(),
                custom_feed.get_description(),
                custom_feed.get_image(),
                None)

        existing = self.get_all_episodes()
        existing_guids = [episode.guid for episode in existing]

        # Insert newly-found episodes into the database + local cache
        new_episodes, seen_guids = custom_feed.get_new_episodes(self, existing_guids)
        self.children.extend(new_episodes)

        self.remove_unreachable_episodes(existing, seen_guids, max_episodes)

    def _consume_updated_feed(self, feed, max_episodes=0):
        self._consume_metadata(feed.get('title', self.url),
                               feed.get('link', self.link),
                               feed.get('description', ''),
                               feed.get('cover_url', None),
                               feed.get('payment_url', None))

        # Update values for HTTP conditional requests
        headers = feed.get('headers', {})
        self.http_etag = headers.get('etag', self.http_etag)
        self.http_last_modified = headers.get('last-modified', self.http_last_modified)

        # Load all episodes to update them properly.
        existing = self.get_all_episodes()

        # We have to sort the entries in descending chronological order,
        # because if the feed lists items in ascending order and has >
        # max_episodes old episodes, new episodes will not be shown.
        # See also: gPodder Bug 1186
        entries = sorted(feed.get('episodes', []), key=lambda episode: episode['published'], reverse=True)

        # We can limit the maximum number of entries that gPodder will parse
        if max_episodes > 0 and len(entries) > max_episodes:
            entries = entries[:max_episodes]

        # GUID-based existing episode list
        existing_guids = dict((e.guid, e) for e in existing)

        # Get most recent published of all episodes
        last_published = self.db.get_last_published(self) or 0
        # fix for #516 an episode was marked published one month in the future (typo in month number)
        # causing every new episode to be marked old
        tomorrow = datetime.datetime.now().timestamp() + self.SECONDS_PER_DAY
        if last_published > tomorrow:
            logger.debug('Episode published in the future for podcast %s', self.title)
            last_published = tomorrow

        # Keep track of episode GUIDs currently seen in the feed
        seen_guids = set()

        # Number of new episodes found
        new_episodes = 0

        # Search all entries for new episodes
        for entry in entries:
            episode = self.EpisodeClass.from_podcastparser_entry(entry, self)
            if episode is not None:
                seen_guids.add(episode.guid)
            else:
                continue

            # Detect (and update) existing episode based on GUIDs
            existing_episode = existing_guids.get(episode.guid, None)
            if existing_episode:
                existing_episode.update_from(episode)
                existing_episode.save()
                continue

            # Workaround for bug 340: If the episode has been
            # published earlier than one week before the most
            # recent existing episode, do not mark it as new.
            if episode.published < last_published - self.SECONDS_PER_WEEK:
                logger.debug('Episode with old date: %s', episode.title)
                episode.is_new = False

            if episode.is_new:
                new_episodes += 1

            # Only allow a certain number of new episodes per update
            if (self.download_strategy == PodcastChannel.STRATEGY_LATEST and
                    new_episodes > 1):
                episode.is_new = False

            episode.save()
            self.children.append(episode)

        self.remove_unreachable_episodes(existing, seen_guids, max_episodes)

    def remove_unreachable_episodes(self, existing, seen_guids, max_episodes):
        # Remove "unreachable" episodes - episodes that have not been
        # downloaded and that the feed does not list as downloadable anymore
        # Keep episodes that are currently being downloaded, though (bug 1534)
        if self.id is not None:
            episodes_to_purge = (e for e in existing if
                    e.state != gpodder.STATE_DOWNLOADED and
                    e.guid not in seen_guids and not e.downloading)

            for episode in episodes_to_purge:
                logger.debug('Episode removed from feed: %s (%s)',
                        episode.title, episode.guid)
                gpodder.user_extensions.on_episode_removed_from_podcast(episode)
                self.db.delete_episode_by_guid(episode.guid, self.id)

                # Remove the episode from the "children" episodes list
                if self.children is not None:
                    self.children.remove(episode)

        # This *might* cause episodes to be skipped if there were more than
        # max_episodes_per_feed items added to the feed between updates.
        # The benefit is that it prevents old episodes from apearing as new
        # in certain situations (see bug #340).
        self.db.purge(max_episodes, self.id)  # TODO: Remove from self.children!

        # Sort episodes by pubdate, descending
        self.children.sort(key=lambda e: e.published, reverse=True)

    def update(self, max_episodes=0):
        try:
            result = self.feed_fetcher.fetch_channel(self)

            if result.status == feedcore.CUSTOM_FEED:
                self._consume_custom_feed(result.feed, max_episodes)
            elif result.status == feedcore.UPDATED_FEED:
                self._consume_updated_feed(result.feed, max_episodes)
            elif result.status == feedcore.NEW_LOCATION:
                url = result.feed
                logger.info('New feed location: %s => %s', self.url, url)
                if url in set(x.url for x in self.model.get_podcasts()):
                    raise Exception('Already subscribed to ' + url)
                self.url = url
                # With the updated URL, fetch the feed again
                self.update(max_episodes)
                return
            elif result.status == feedcore.NOT_MODIFIED:
                pass

            self.save()
        except Exception as e:
            #  "Not really" errors
            # feedcore.AuthenticationRequired
            #  Temporary errors
            # feedcore.Offline
            # feedcore.BadRequest
            # feedcore.InternalServerError
            # feedcore.WifiLogin
            #  Permanent errors
            # feedcore.Unsubscribe
            # feedcore.NotFound
            # feedcore.InvalidFeed
            # feedcore.UnknownStatusCode
            gpodder.user_extensions.on_podcast_update_failed(self, e)
            raise

        gpodder.user_extensions.on_podcast_updated(self)

        # Re-determine the common prefix for all episodes
        self._determine_common_prefix()

        self.db.commit()

    def delete(self):
        self.db.delete_podcast(self)
        self.model._remove_podcast(self)

    def save(self):
        if self.download_folder is None:
            self.get_save_dir()

        gpodder.user_extensions.on_podcast_save(self)

        self.save_ext_data()
        self.db.save_podcast(self)
        self.model._append_podcast(self)

    def get_statistics(self):
        if self.id is None:
            return (0, 0, 0, 0, 0)
        else:
            return self.db.get_podcast_statistics(self.id)

    @property
    def group_by(self):
        if not self.section:
            self.section = self._get_content_type()
            self.save()

        return self.section

    def _get_content_type(self):
        if 'youtube.com' in self.url or 'vimeo.com' in self.url or 'escapistmagazine.com' in self.url:
            return _('Video')

        audio, video, other = 0, 0, 0
        for content_type in self.db.get_content_types(self.id):
            content_type = content_type.lower()
            if content_type.startswith('audio'):
                audio += 1
            elif content_type.startswith('video'):
                video += 1
            else:
                other += 1

        if audio >= video:
            return _('Audio')
        elif video > other:
            return _('Video')

        return _('Other')

    def authenticate_url(self, url):
        return util.url_add_authentication(url, self.auth_username, self.auth_password)

    def rename(self, new_title):
        new_title = new_title.strip()
        if self.title == new_title:
            return

        new_folder_name = self.find_unique_folder_name(new_title)
        if new_folder_name and new_folder_name != self.download_folder:
            new_folder = os.path.join(gpodder.downloads, new_folder_name)
            old_folder = os.path.join(gpodder.downloads, self.download_folder)
            if os.path.exists(old_folder):
                if not os.path.exists(new_folder):
                    # Old folder exists, new folder does not -> simply rename
                    logger.info('Renaming %s => %s', old_folder, new_folder)
                    os.rename(old_folder, new_folder)
                else:
                    # Both folders exist -> move files and delete old folder
                    logger.info('Moving files from %s to %s', old_folder,
                            new_folder)
                    for file in glob.glob(os.path.join(old_folder, '*')):
                        shutil.move(file, new_folder)
                    logger.info('Removing %s', old_folder)
                    shutil.rmtree(old_folder, ignore_errors=True)
            self.download_folder = new_folder_name

        self.title = new_title
        self.save()

    def _determine_common_prefix(self):
        # We need at least 2 episodes for the prefix to be "common" ;)
        if len(self.children) < 2:
            self._common_prefix = ''
            return

        prefix = os.path.commonprefix([x.title for x in self.children])
        # The common prefix must end with a space - otherwise it's not
        # on a word boundary, and we might end up chopping off too much
        if prefix and prefix[-1] != ' ':
            prefix = prefix[:prefix.rfind(' ') + 1]

        self._common_prefix = prefix

    def get_all_episodes(self):
        return self.children

    def get_episodes(self, state):
        return [e for e in self.get_all_episodes() if e.state == state]

    def find_unique_folder_name(self, download_folder):
        # Remove trailing dots to avoid errors on Windows (bug 600)
        # Also remove leading dots to avoid hidden folders on Linux
        download_folder = download_folder.strip('.' + string.whitespace)

        for folder_name in util.generate_names(download_folder):
            if (not self.db.podcast_download_folder_exists(folder_name) or
                    self.download_folder == folder_name):
                return folder_name

    def get_save_dir(self, force_new=False):
        if self.download_folder is None or force_new:
            # we must change the folder name, because it has not been set manually
            fn_template = util.sanitize_filename(self.title, self.MAX_FOLDERNAME_LENGTH)

            if not fn_template:
                fn_template = util.sanitize_filename(self.url, self.MAX_FOLDERNAME_LENGTH)

            # Find a unique folder name for this podcast
            download_folder = self.find_unique_folder_name(fn_template)

            # Try removing the download folder if it has been created previously
            if self.download_folder is not None:
                folder = os.path.join(gpodder.downloads, self.download_folder)
                try:
                    os.rmdir(folder)
                except OSError:
                    logger.info('Old download folder is kept for %s', self.url)

            logger.info('Updating download_folder of %s to %s', self.url,
                    download_folder)
            self.download_folder = download_folder
            self.save()

        save_dir = os.path.join(gpodder.downloads, self.download_folder)

        # Create save_dir if it does not yet exist
        if not util.make_directory(save_dir):
            logger.error('Could not create save_dir: %s', save_dir)

        return save_dir

    save_dir = property(fget=get_save_dir)

    def remove_downloaded(self):
        # Remove the download directory
        for episode in self.get_episodes(gpodder.STATE_DOWNLOADED):
            filename = episode.local_filename(create=False, check_only=True)
            if filename is not None:
                gpodder.user_extensions.on_episode_delete(episode, filename)

        shutil.rmtree(self.save_dir, True)

    @property
    def cover_file(self):
        return os.path.join(self.save_dir, 'folder')


class Model(object):
    PodcastClass = PodcastChannel

    def __init__(self, db):
        self.db = db
        self.children = None

    def _append_podcast(self, podcast):
        if podcast not in self.children:
            self.children.append(podcast)

    def _remove_podcast(self, podcast):
        self.children.remove(podcast)
        gpodder.user_extensions.on_podcast_delete(self)

    def get_podcasts(self):
        def podcast_factory(dct, db):
            return self.PodcastClass.create_from_dict(dct, self, dct['id'])

        if self.children is None:
            self.children = self.db.load_podcasts(podcast_factory)

            # Check download folders for changes (bug 902)
            for podcast in self.children:
                podcast.check_download_folder()

        return self.children

    def get_podcast(self, url):
        for p in self.get_podcasts():
            if p.url == url:
                return p
        return None

    def load_podcast(self, url, create=True, authentication_tokens=None,
                     max_episodes=0):
        assert all(url != podcast.url for podcast in self.get_podcasts())
        return self.PodcastClass.load(self, url, create,
                                      authentication_tokens,
                                      max_episodes)

    @classmethod
    def podcast_sort_key(cls, podcast):
        return cls.PodcastClass.sort_key(podcast)

    @classmethod
    def episode_sort_key(cls, episode):
        return episode.published

    @classmethod
    def sort_episodes_by_pubdate(cls, episodes, reverse=False):
        """Sort a list of PodcastEpisode objects chronologically

        Returns a iterable, sorted sequence of the episodes
        """
        return sorted(episodes, key=cls.episode_sort_key, reverse=reverse)


def check_root_folder_path():
    root = gpodder.home
    if gpodder.ui.win32:
        longest = len(root) \
            + 1 + PodcastChannel.MAX_FOLDERNAME_LENGTH \
            + 1 + PodcastEpisode.MAX_FILENAME_WITH_EXT_LENGTH
        if longest > 260:
            return _("Warning: path to gPodder home (%(root)s) is very long "
                     "and can result in failure to download files.\n" % {"root": root}) \
                + _("You're advised to set it to a shorter path.")
    return None
