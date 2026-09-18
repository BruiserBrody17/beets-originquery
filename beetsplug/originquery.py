import confuse
import glob
import json
import jsonpath_rw
import os
import re
import shutil
import sys
import yaml
from collections import OrderedDict
from beets import config, ui
from beets.util import MoveOperation, get_most_common_tags, syspath
from beets.plugins import BeetsPlugin
from pathlib import Path

BEETS_TO_LABEL = OrderedDict([
    ('album', 'Release name'),
    ('artist', 'Artist'),
    ('media', 'Media'),
    ('year', 'Edition year'),
    ('country', 'Country'),
    ('label', 'Record label'),
    ('catalognum', 'Catalog number'),
    ('albumdisambig', 'Edition'),
    ('genres', 'Genres'),
])

# Conflicts will be reported if any of these fields don't match.
CONFLICT_FIELDS = ['catalognum', 'media']

# These fields feed the primary MusicBrainz search phrase (release/artist) or
# are otherwise never MusicBrainz search filters, so origin data overrides
# them whenever present, regardless of the configured extra_tags.
ALWAYS_APPLY_FIELDS = ['album', 'artist', 'genres']

# Fields not computed by beets.util.get_most_common_tags; the plugin derives
# their current "tagged" value itself instead.
NON_LIKELY_FIELDS = ['genres']


def escape_braces(string):
    return string.replace('{', '{{').replace('}', '}}')


def normalize_catno(catno):
    return catno.upper().replace(' ', '').replace('-', '')


def clean_genre_tag(tag):
    # Tracker-style tag slugs use dots for multi-word tags (e.g.
    # "classic.rock"); numeric-leading tokens like "1970s" are left alone.
    tag = tag.replace('.', ' ')
    return re.sub(r'\b[a-zA-Z]+\b', lambda m: m.group(0).capitalize(), tag)


def sanitize_value(key, value):
    if key == 'media' and value == 'WEB':
        return 'Digital Media'
    if key == 'catalognum' or key == 'label':
        return re.split('[,/]', value)[0].strip()
    if key == 'year' and value == '0':
        return ''
    if key == 'genres':
        tags = [clean_genre_tag(t.strip()) for t in value.split(',') if t.strip()]
        return '; '.join(tags)
    return value


def highlight(text, active=True):
    if active:
        return ui.colorize('text_highlight_minor', text)
    return text


class OriginQuery(BeetsPlugin):
    def __init__(self):
        super(OriginQuery, self).__init__()

        def fail(msg):
            self.error(msg)
            self.error('Plugin disabled.')

        try:
            self.extra_tags = config['musicbrainz']['extra_tags'].get()
        except confuse.NotFoundError:
            return fail('This version of beets does not support extra query tags.')

        if not len(self.extra_tags):
            return fail('Config error: musicbrainz.extra_tags not set.')

        config_patterns = None
        try:
            config_patterns = self.config['tag_patterns'].get()
            if not isinstance(config_patterns, dict):
                raise confuse.ConfigError()
        except confuse.ConfigError:
            return fail('Config error: originquery.tag_patterns must be set to a dictionary of key -> pattern mappings.')

        try:
            self.origin_file = Path(self.config['origin_file'].get())
        except confuse.NotFoundError:
            return fail('Config error: originquery.origin_file not set.')
        self.tag_patterns = {}

        try:
            origin_type = self.config['origin_type'].as_choice(['yaml', 'json', 'text']).lower()
        except confuse.NotFoundError:
            origin_type = self.origin_file.suffix.lower()[1:]

        if origin_type == 'json':
            self.match_fn = self.match_json
        elif origin_type == 'yaml':
            self.match_fn = self.match_yaml
        else:
            self.match_fn = self.match_text

        for key, pattern in config_patterns.items():
            if key not in BEETS_TO_LABEL:
                return fail('Config error: unknown key "{0}"'.format(key))
                self.error('Plugin disabled.')

            if origin_type == 'json' or origin_type == 'yaml':
                try:
                    self.tag_patterns[key] = jsonpath_rw.parse(pattern)
                except Exception as e:
                    return fail('Config error: invalid tag pattern for "{0}". "{1}" is not a valid JSON path ({2}).'
                                .format(key, pattern, format(str(e))))
                continue

            try:
                regex = re.compile(pattern)
                self.tag_patterns[key] = regex
            except re.error as e:
                return fail('Config error: invalid tag pattern for "{0}". "{1}" is not a valid regex ({2}).'
                            .format(key, pattern, format(str(e))))
            if regex.groups != 1:
                return fail('Config error: invalid tag pattern for "{0}". "{1}" must have exactly one capture group.'
                            .format(key, pattern))

        self.register_listener('import_task_start', self.import_task_start)
        # task.add() (called between these two events) creates the Album
        # object from the chosen candidate's own MB data and, via
        # Album.store(inherit=True), pushes every album-level field (media,
        # year, label, catalognum, albumdisambig, genres, ...) back down to
        # every item -- overwriting whatever we set at import_task_start.
        # Re-apply origin data to both the album and its items here, after
        # that clobbering has already happened, then force a real re-write
        # so the correction actually reaches the files (manipulate_files
        # already wrote the -- wrong -- first pass by this point).
        self.register_listener('import_task_files', self.import_task_files)
        self.tasks = {}

        try:
            self.use_origin_on_conflict = self.config['use_origin_on_conflict'].get(bool)
        except confuse.NotFoundError:
            self.use_origin_on_conflict = False


    def error(self, msg):
        self._log.error(escape_braces(ui.colorize('text_error', msg)))


    def warn(self, msg):
        self._log.warning(escape_braces(ui.colorize('text_warning', msg)))


    def info(self, msg):
        # beets defaults to log level warning for event handlers.
        self._log.warning(escape_braces(msg))


    def print_tags(self, items, use_tagged):
        headers = ['Field', 'Tagged Data', 'Origin Data']

        w_key = max(len(headers[0]), *(len(BEETS_TO_LABEL[k]) for k, v in items))
        w_tagged = max(len(headers[1]), *(len(v['tagged']) for k, v in items))
        w_origin = max(len(headers[2]), *(len(v['origin']) for k, v in items))

        self.info('╔{0}╤{1}╤{2}╗'.format('═' * (w_key + 2), '═' * (w_tagged + 2), '═' * (w_origin + 2)))
        self.info('║ {0} │ {1} │ {2} ║'.format(headers[0].ljust(w_key),
                                               highlight(headers[1].ljust(w_tagged), use_tagged),
                                               highlight(headers[2].ljust(w_origin), not use_tagged)))
        self.info('╟{0}┼{1}┼{2}╢'.format('─' * (w_key + 2), '─' * (w_tagged + 2), '─' * (w_origin + 2)))
        for k, v in items:
            if not v['tagged'] and not v['origin']:
                continue
            tagged_active = use_tagged and v['active']
            origin_active = not use_tagged and v['active']
            self.info('║ {0} │ {1} │ {2} ║'.format(BEETS_TO_LABEL[k].ljust(w_key),
                                                   highlight(v['tagged'].ljust(w_tagged), tagged_active),
                                                   highlight(v['origin'].ljust(w_origin), origin_active)))
        self.info('╚{0}╧{1}╧{2}╝'.format('═' * (w_key + 2), '═' * (w_tagged + 2), '═' * (w_origin + 2)))


    def match_text(self, origin_path):
        with open(origin_path, encoding="utf-8") as f:
            lines = f.readlines()

        for key, pattern in self.tag_patterns.items():
            for line in lines:
                line = line.strip()
                match = re.match(pattern, line)
                if not match:
                    continue
                yield key, match[1]


    def match_json(self, origin_path):
        with open(origin_path, encoding="utf-8") as f:
            data = json.load(f)

        for key, pattern in self.tag_patterns.items():
            match = pattern.find(data)
            if not len(match):
                continue

            yield key, str(match[0].value)


    def match_yaml(self, origin_path):
        with open(origin_path, encoding="utf-8") as f:
            data = yaml.load(f, Loader=yaml.SafeLoader)

        for key, pattern in self.tag_patterns.items():
            match = pattern.find(data)
            if not len(match) or not match[0].value:
                continue
            yield key, str(match[0].value)


    def import_task_start(self, task, session):
        task_info = self.tasks[task] = {}

        # In case this is a multi-disc import, find the common parent directory.
        base = os.path.commonpath(task.paths).decode('utf8')

        glob_pattern = os.path.join(glob.escape(base), self.origin_file)
        origin_glob = sorted(glob.glob(glob_pattern))
        if len(origin_glob) < 1:
            task_info['origin_path'] = Path(base) / self.origin_file
            task_info['missing_origin'] = True
            self.warn('No origin file found at {0}'.format(task_info['origin_path']))
            return
        task_info['origin_path'] = origin_path = Path(origin_glob[0])

        conflict = False
        likelies = get_most_common_tags(task.items)
        task_info['tag_compare'] = tag_compare = OrderedDict()
        for tag in BEETS_TO_LABEL:
            if tag in NON_LIKELY_FIELDS:
                current = (task.items[0].get(tag) if task.items else None) or []
                tagged = '; '.join(current) if isinstance(current, list) else str(current)
            else:
                tagged = str(likelies[tag])
            tag_compare.update({tag: {
                'tagged': tagged,
                'active': tag in self.extra_tags or tag in ALWAYS_APPLY_FIELDS,
                'origin': '',
            }})

        for key, value in self.match_fn(origin_path):
            if tag_compare[key]['origin']:
                continue

            tagged_value = tag_compare[key]['tagged']
            origin_value = sanitize_value(key, value)
            tag_compare[key]['origin'] = origin_value
            if key not in CONFLICT_FIELDS or not tagged_value or not origin_value:
                continue

            if key == 'catalognum':
                tagged_value = normalize_catno(tagged_value)
                origin_value = normalize_catno(origin_value)

            if tagged_value != origin_value:
                conflict = task_info['conflict'] = True

        task_info['apply_origin'] = not conflict or self.use_origin_on_conflict
        if task_info['apply_origin']:
            self._apply_origin_values(tag_compare, task.items)
            for item in task.items:
                # beets weighs media heavily, and will even prioritize a media match over an exact catalognum match.
                # At the same time, media for uploaded music is often mislabeled (e.g., Enhanced CD and SACD are just
                # grouped as CD). This does not make a good combination. As a workaround, lower the weight for media
                # if we also have a catalognum.
                if item['media'] and item['catalognum']:
                    config['match']['distance_weights']['media'] = .2

        self.info('Using origin file {0}'.format(origin_path))
        use_tagged = conflict and not self.use_origin_on_conflict
        self.print_tags(task_info.get('tag_compare').items(), use_tagged)
        if conflict:
            self.warn("Origin data conflicts with tagged data.")


    def _apply_origin_values(self, tag_compare, items):
        for item in items:
            for tag, entry in tag_compare.items():
                origin_value = entry['origin']
                if tag in ALWAYS_APPLY_FIELDS:
                    # Only override the search phrase when origin data
                    # actually supplies a value; otherwise leave the
                    # tagged album/artist alone.
                    if not origin_value:
                        continue
                elif tag not in self.extra_tags:
                    continue
                if tag == 'year' and origin_value:
                    origin_value = int(origin_value) if origin_value.isdigit() else ''
                item[tag] = origin_value


    def import_task_files(self, task, session):
        task_info = self.tasks.get(task)
        if not task_info:
            return

        tag_compare = task_info.get('tag_compare')
        if task_info.get('apply_origin') and tag_compare:
            self._apply_origin_values(tag_compare, task.items)

            album = getattr(task, 'album', None)
            if album is not None:
                # Only genuine album-level fields apply here (e.g. `artist`
                # and `media` are item-only and would otherwise end up as
                # stray flexible attributes on the album).
                album_tag_compare = OrderedDict(
                    (tag, entry) for tag, entry in tag_compare.items()
                    if tag in album._fields
                )
                self._apply_origin_values(album_tag_compare, [album])
                # inherit=True pushes these corrected album-level fields
                # back down to every item's DB row (not just the in-memory
                # object).
                album.store()

            # The destination path template can reference these corrected
            # fields (e.g. $albumdisambig), but manipulate_files() already
            # moved/copied each item to a path computed *before* this
            # correction. Relocate to the now-correct destination -- this
            # only touches our own already-copied output tree, never the
            # original source files.
            #
            # %aunique{}/%sunique{} memoize their result per album/item id
            # (not per field value) in lib._memotable, populated by the
            # first path computation manipulate_files() already did using
            # the *pre-correction* field values. Recomputing the path now,
            # after correcting those fields, would silently reuse that
            # stale memo (e.g. "no collision" computed against the raw MB
            # title, even though the origin-corrected title does collide
            # with another album) unless the cache is invalidated first --
            # same as what Library.add() itself does whenever the album
            # set changes.
            db = getattr(task.items[0], '_db', None) if task.items else None
            if db is not None:
                db._memotable = {}
            for item in task.items:
                item.move(operation=MoveOperation.MOVE, with_album=False)
                item.try_write()
                item.store()
            if album is not None:
                album.move_art(operation=MoveOperation.MOVE)
                album.store()

        self._copy_origin_file(task, task_info)


    def _copy_origin_file(self, task, task_info):
        """Carry the source origin file through into the destination album
        directory, for reference alongside the imported files."""
        if task_info.get('missing_origin'):
            return
        origin_path = task_info.get('origin_path')
        if not origin_path or not origin_path.exists():
            return

        album = getattr(task, 'album', None)
        try:
            if album is not None:
                dest_dir = album.item_dir()
            else:
                dest_dir = os.path.dirname(task.items[0].path)
        except (ValueError, AttributeError, IndexError):
            return

        dest = os.path.join(dest_dir, origin_path.name.encode('utf-8'))
        try:
            shutil.copyfile(str(origin_path), syspath(dest))
        except OSError as exc:
            self.warn('Could not copy origin file to destination: {0}'.format(exc))
