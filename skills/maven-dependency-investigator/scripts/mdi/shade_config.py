"""Observe shading configuration in captured POM bytes, without Maven execution.

This is a raw, namespace-exact XML-shape reader, not a Maven Shade evaluator.
Recognized shapes are configuration/relocations/relocation, artifactSet, filters,
and their literal include/exclude values. No plugin-version grammar, glob rule,
inheritance, execution merging, profile activation or property interpolation is
assumed. An absent groupId produces a default-group candidate, not an observed
resolved plugin identity. Foreign groups do not match.

inspect_shade_config returns configurations independently at plugin and execution
level, with source locators and the exact captured-byte digest. Optional producer
{tool, version} and observed_at are caller-supplied capture labels, not verified
claims that a plugin ran. The parser does no IO and generates no observation time.
Optional observed_paths is a bounded list of {original, packaged} path pairs.
Literal package-prefix correspondence is only a candidate connection to observed
configuration, never binary relocation, component identity, or build provenance.
Even matching paths do not evaluate the recorded include/exclude expressions.
"""

import hashlib
import re
from dataclasses import dataclass
from itertools import chain

# Reuse the declaration reader's namespace and positional-locator conventions.
from mdi.discover import _kids, _root_problem, _split_tag, _tag_positions
from mdi.limits import Limits
from mdi.xmlsafe import UnsafeXML, parse_xml


@dataclass(frozen=True)
class ConfigLimits:
    max_bytes: int = 8 * 1024 * 1024
    max_nodes: int = 50000
    max_text: int = 4096
    max_records: int = 4096
    max_paths: int = 256

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) is not int or value <= 0:
                raise ValueError(f'{name} must be a positive integer')


class ConfigError(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


_LIMITATIONS = [
    'raw configuration observations only; Maven and plugin execution are not proven',
    'inheritance, pluginManagement activation and execution configuration merging are not evaluated',
    'profile activation is unknown; properties and include/exclude expressions remain uninterpreted',
    'configuration and path correspondence do not prove binary relocation, artifact identity or build provenance',
    'unrecognized configuration shapes are not interpreted; missing records do not prove absence of shading',
]
_PACKAGE = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*\Z')


def _text_value(value, label, limits, nullable=False):
    if value is None and nullable:
        return value
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ConfigError(f'{label} must be nonempty text', 'invalid-options')
    if len(value) > limits.max_text:
        raise ConfigError(f'{label} exceeds text limit', 'text-limit')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ConfigError(f'{label} is not UTF-8 text', 'invalid-options') from None
    return value


class _Reader:
    def __init__(self, root, source_path, limits):
        self.ns = _split_tag(root.tag)[0]
        self.source_path = source_path
        self.limits = limits
        self.issues = []
        self.records = 0
        self.paths = {id(root): 'project'}
        stack = [root]
        count = 0
        admitted = 1
        while stack:
            node = stack.pop()
            count += 1
            if count > limits.max_nodes:
                raise ConfigError('XML node limit exceeded', 'node-limit')
            if len(node) > limits.max_nodes - admitted:
                raise ConfigError('XML node limit exceeded', 'node-limit')
            admitted += len(node)
            for value in (node.text, node.tail):
                if value is not None and len(value) > limits.max_text:
                    raise ConfigError('XML text limit exceeded', 'text-limit')
            for key, value in node.attrib.items():
                if len(key) > limits.max_text or len(value) > limits.max_text:
                    raise ConfigError('XML attribute limit exceeded', 'text-limit')
            if isinstance(node.tag, str) and len(node.tag) > limits.max_text:
                raise ConfigError('XML tag limit exceeded', 'text-limit')
            positions = _tag_positions(node)
            parent_path = self.paths[id(node)]
            for child in node:
                if isinstance(child.tag, str):
                    ordinal = positions[id(child)]
                    suffix = f'[{ordinal + 1}]' if ordinal else ''
                    locator = f'{parent_path}/{_split_tag(child.tag)[1]}{suffix}'
                    if len(locator) > limits.max_text:
                        raise ConfigError('XML locator limit exceeded', 'text-limit')
                    self.paths[id(child)] = locator
                else:
                    self.paths[id(child)] = parent_path
                stack.append(child)
        self.nodes = count

    def source(self, node):
        return {'pom': self.source_path, 'xml_path': self.paths[id(node)]}

    def tick(self):
        self.records += 1
        if self.records > self.limits.max_records:
            raise ConfigError('configuration record limit exceeded', 'record-limit')

    def issue(self, code, node):
        self.tick()
        self.issues.append({'code': code, 'source': self.source(node)})

    def value(self, node):
        if any(isinstance(child.tag, str) for child in node):
            self.issue('non-scalar-field', node)
            return None
        fragments = []
        total = 0
        for fragment in chain((node.text,), (child.tail for child in node)):
            if fragment:
                total += len(fragment)
                if total > self.limits.max_text:
                    raise ConfigError('scalar text limit exceeded', 'text-limit')
                fragments.append(fragment)
        text = ''.join(fragments)
        text = text.strip() or None
        if text is not None and '${' in text:
            self.issue('unresolved-property', node)
        return text

    def scalar(self, node, name):
        children = _kids(node, name, self.ns)
        if len(children) > 1:
            self.issue('duplicate-scalar', node)
            return None
        return self.value(children[0]) if children else None

    def values(self, node, plural, singular):
        result = []
        for group in _kids(node, plural, self.ns):
            for child in _kids(group, singular, self.ns):
                self.tick()
                result.append({'value': self.value(child), 'source': self.source(child)})
        return result

    def configurations(self, plugin, context, observed_paths):
        found = []
        scopes = [(plugin, None)]
        for executions in _kids(plugin, 'executions', self.ns):
            for execution in _kids(executions, 'execution', self.ns):
                self.tick()
                scopes.append((execution, {
                    'id': self.scalar(execution, 'id'), 'phase': self.scalar(execution, 'phase'),
                    'goals': self.values(execution, 'goals', 'goal'), 'source': self.source(execution),
                }))
        for scope, execution in scopes:
            for config in _kids(scope, 'configuration', self.ns):
                self.tick()
                row = dict(context, source=self.source(config), execution=execution,
                           execution_proven=False, relocations=[], artifact_sets=[], filters=[])
                for group in _kids(config, 'relocations', self.ns):
                    for relocation in _kids(group, 'relocation', self.ns):
                        self.tick()
                        pattern = self.scalar(relocation, 'pattern')
                        shaded = self.scalar(relocation, 'shadedPattern')
                        if not pattern or not shaded:
                            self.issue('incomplete-relocation', relocation)
                        links = []
                        if pattern and shaded and _PACKAGE.fullmatch(pattern) and _PACKAGE.fullmatch(shaded):
                            original_prefix = pattern.replace('.', '/') + '/'
                            packaged_prefix = shaded.replace('.', '/') + '/'
                            for pair in observed_paths:
                                original, packaged = pair['original'], pair['packaged']
                                if (original.startswith(original_prefix)
                                        and packaged.startswith(packaged_prefix)
                                        and original[len(original_prefix):]
                                        and original[len(original_prefix):] == packaged[len(packaged_prefix):]):
                                    self.tick()
                                    links.append(dict(pair, basis='literal-prefix-candidate', relocation_proven=False))
                        row['relocations'].append({
                            'pattern': pattern, 'shaded_pattern': shaded,
                            'includes': self.values(relocation, 'includes', 'include'),
                            'excludes': self.values(relocation, 'excludes', 'exclude'),
                            'source': self.source(relocation), 'path_candidates': links,
                        })
                for aset in _kids(config, 'artifactSet', self.ns):
                    self.tick()
                    row['artifact_sets'].append({
                        'includes': self.values(aset, 'includes', 'include'),
                        'excludes': self.values(aset, 'excludes', 'exclude'), 'source': self.source(aset),
                    })
                for filters in _kids(config, 'filters', self.ns):
                    for filt in _kids(filters, 'filter', self.ns):
                        self.tick()
                        row['filters'].append({
                            'artifact': self.scalar(filt, 'artifact'),
                            'includes': self.values(filt, 'includes', 'include'),
                            'excludes': self.values(filt, 'excludes', 'exclude'), 'source': self.source(filt),
                        })
                found.append(row)
        return found


def inspect_shade_config(data, *, source_path, module=None, observed_at=None,
                         producer=None, observed_paths=(), limits=ConfigLimits()):
    """Return raw captured-POM configuration evidence; no paths are opened."""
    if not isinstance(limits, ConfigLimits):
        raise ConfigError('limits must be ConfigLimits', 'invalid-options')
    if not isinstance(data, bytes):
        raise ConfigError('captured input must be bytes', 'invalid-options')
    _text_value(source_path, 'source_path', limits)
    _text_value(module, 'module', limits, nullable=True)
    _text_value(observed_at, 'observed_at', limits, nullable=True)
    if producer is not None:
        if not isinstance(producer, dict) or set(producer) != {'tool', 'version'}:
            raise ConfigError('producer requires tool and version only', 'invalid-options')
        producer = {k: _text_value(v, f'producer.{k}', limits, nullable=True) for k, v in producer.items()}
    if not isinstance(observed_paths, (list, tuple)) or len(observed_paths) > limits.max_paths:
        raise ConfigError('observed path count exceeds limit or has wrong type', 'path-limit')
    pairs = []
    for pair in observed_paths:
        if not isinstance(pair, dict) or set(pair) != {'original', 'packaged'}:
            raise ConfigError('path pair requires original and packaged only', 'invalid-options')
        pairs.append({k: _text_value(v, k, limits) for k, v in pair.items()})
    try:
        root = parse_xml(data, limits=Limits(xml_bytes=limits.max_bytes))
    except UnsafeXML as exc:
        raise ConfigError('captured POM rejected by safe XML parser', 'unsafe-xml') from exc
    problem = _root_problem(root)
    if problem is not None:
        raise ConfigError(problem, 'not-project')
    reader = _Reader(root, source_path, limits)
    scopes = [(root, None)]
    for profiles in _kids(root, 'profiles', reader.ns):
        identified = [(p, reader.scalar(p, 'id')) for p in _kids(profiles, 'profile', reader.ns)]
        counts = {}
        for _, pid in identified:
            if pid is not None:
                counts[pid] = counts.get(pid, 0) + 1
        for profile, pid in identified:
            prefix = reader.paths[id(profile)]
            if pid is not None and "'" not in pid and counts[pid] == 1:
                replacement = reader.paths[id(profiles)] + f"/profile[@id='{pid}']"
                for node in profile.iter():
                    current = reader.paths[id(node)]
                    locator = replacement + current[len(prefix):]
                    if len(locator) > limits.max_text:
                        raise ConfigError('profile locator limit exceeded', 'text-limit')
                    reader.paths[id(node)] = locator
            scopes.append((profile, {'id': pid, 'source': reader.paths[id(profile)], 'activation': 'unknown'}))
    configurations, plugins = [], 0
    for container, profile in scopes:
        for build in _kids(container, 'build', reader.ns):
            sections = [(build, False)] + [(m, True) for m in _kids(build, 'pluginManagement', reader.ns)]
            for section, management in sections:
                for group in _kids(section, 'plugins', reader.ns):
                    for plugin in _kids(group, 'plugin', reader.ns):
                        artifact = reader.scalar(plugin, 'artifactId')
                        group_id = reader.scalar(plugin, 'groupId')
                        if group_id is None and _kids(plugin, 'groupId', reader.ns):
                            reader.issue('invalid-plugin-group', plugin)
                            continue
                        if artifact != 'maven-shade-plugin' or group_id not in (None, 'org.apache.maven.plugins'):
                            continue
                        reader.tick()
                        plugins += 1
                        if group_id is None:
                            reader.issue('unresolved-plugin-group', plugin)
                        context = {
                            'module': module, 'plugin': {'groupId': group_id, 'artifactId': artifact,
                                                       'version': reader.scalar(plugin, 'version')},
                            'plugin_identity': 'candidate-default-group' if group_id is None else 'declared-coordinate',
                            'plugin_source': reader.source(plugin), 'profile': profile,
                            'management_only': management,
                        }
                        configurations.extend(reader.configurations(plugin, context, pairs))
    return {
        'kind': 'observed-shade-configuration',
        'capture': {'path': source_path, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                    'observed_at': observed_at, 'producer': producer, 'provenance_basis': 'caller-supplied'},
        'configurations': configurations, 'issues': reader.issues,
        'artifact_identity_proven': False, 'relocation_proven': False,
        'coverage': {'status': 'partial' if reader.issues else 'complete-within-captured-pom',
                     'xml_nodes': reader.nodes, 'plugins_observed': plugins,
                     'configurations_observed': len(configurations), 'limitations': list(_LIMITATIONS)},
    }
