"""Pure OCI layer visibility composition regressions."""
import copy
import hashlib
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/maven-dependency-investigator/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mdi.oci_visibility import VisibilityError, VisibilityLimits, compose_visibility


def member(name, index=0, kind='30', status='recorded', content=b'x'):
    return dict(index=index, name=name, type=kind, status=status, name_truncated=False,
                size_declared=len(content), size_actual=len(content),
                sha256=hashlib.sha256(content).hexdigest(), role='unknown')


def layer(index, names, *, complete=True, archives=None):
    members = [member(n, i, content=b'' if n.rsplit('/', 1)[-1].startswith('.wh.') else b'x')
               if isinstance(n, str) else dict(n, index=i) for i, n in enumerate(names)]
    return dict(index=index, digest='sha256:' + str(index % 10) * 64,
                distribution=dict(members=members, archives=archives or [], issues=[],
                                  coverage={'status': 'complete' if complete else 'partial'}),
                whiteouts=[], presence_scope='layer-blob-only', realm='unknown')


def inventory(*layers):
    return dict(selected_manifest='sha256:'+'a'*64, layers=list(layers),
                issues=[{'code': 'layer-composition-not-performed'}],
                coverage={'status': 'partial', 'merged_filesystem': False})


def rows(result):
    return {(r['layer_index'], r['path']): r for r in result['entries']}


class VisibilityTests(unittest.TestCase):
    def test_regular_replacement_preserves_both_provenance_records(self):
        result = compose_visibility(inventory(layer(0, ['a.jar']), layer(1, ['a.jar'])))
        data = rows(result)
        self.assertEqual(data[0, 'a.jar']['visibility'], 'removed')
        self.assertEqual(data[1, 'a.jar']['visibility'], 'visible')
        self.assertEqual(data[0, 'a.jar']['changed_by'], {'layer_index': 1, 'member_index': 0})
        self.assertEqual(result['coverage']['status'], 'complete-within-recorded-layers')
        self.assertEqual(result['realm'], 'unknown')

    def test_whiteout_removes_lower_subtree_not_same_layer_additions(self):
        for names in (['.wh.lib', 'lib/new.jar'], ['lib/new.jar', '.wh.lib']):
            result = compose_visibility(inventory(layer(0, ['lib/old.jar']), layer(1, names)))
            data = rows(result)
            self.assertEqual(data[0, 'lib/old.jar']['visibility'], 'removed')
            self.assertEqual(data[1, 'lib/new.jar']['visibility'], 'visible')
            self.assertEqual(data[1, '.wh.lib']['reason'], 'whiteout-marker')

    def test_opaque_hides_all_lower_descendants_but_not_directory(self):
        result = compose_visibility(inventory(
            layer(0, [member('lib', kind='35', content=b''), 'lib/a', 'lib/sub/b', 'other']),
            layer(1, ['lib/new', 'lib/.wh..wh..opq'])))
        data = rows(result)
        self.assertEqual(data[0, 'lib']['visibility'], 'visible')
        self.assertEqual(data[0, 'other']['visibility'], 'visible')
        self.assertEqual(data[0, 'lib/sub/b']['visibility'], 'removed')
        self.assertEqual(data[1, 'lib/new']['visibility'], 'visible')

    def test_root_opaque(self):
        result = compose_visibility(inventory(layer(0, ['a', 'd/b']), layer(1, ['new', '.wh..wh..opq'])))
        self.assertEqual([r['path'] for r in result['entries'] if r['visibility']=='visible'], ['new'])

    def test_file_replaces_directory_and_directory_replaces_file(self):
        result = compose_visibility(inventory(
            layer(0, [member('a', kind='35', content=b''), 'a/old', 'b']),
            layer(1, ['a', member('b', kind='35', content=b''), 'b/new'])))
        data = rows(result)
        self.assertEqual(data[0, 'a/old']['visibility'], 'removed')
        self.assertEqual(data[0, 'b']['visibility'], 'removed')
        self.assertEqual(data[1, 'b/new']['visibility'], 'visible')

    def test_directory_to_directory_keeps_lower_children(self):
        result = compose_visibility(inventory(layer(0, [member('d', kind='35', content=b''), 'd/a']),
                                               layer(1, [member('d', kind='35', content=b'')])))
        self.assertEqual(rows(result)[0, 'd/a']['visibility'], 'visible')

    def test_duplicate_paths_do_not_choose_first_or_last(self):
        result = compose_visibility(inventory(layer(0, ['a']), layer(1, ['a', 'a'])))
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertTrue(all(r['visibility']=='uncertain' for r in result['entries']))
        self.assertIn('duplicate-member-path', {i['code'] for i in result['issues']})

    def test_partial_layer_invalidates_absence_and_final_presence(self):
        result = compose_visibility(inventory(layer(0, ['a']), layer(1, ['.wh.a'], complete=False)))
        self.assertEqual(rows(result)[0, 'a']['visibility'], 'uncertain')
        self.assertFalse(result['coverage']['definitive_absence'])
        self.assertEqual(result['java_candidates'], [])

    def test_missing_layer_and_invalid_root_are_not_complete(self):
        result = compose_visibility(inventory(layer(0, ['a']), layer(2, ['b'])))
        self.assertIn('missing-layer-inventory', {i['code'] for i in result['issues']})
        self.assertEqual(result['coverage']['status'], 'partial')
        data = inventory()
        data['selected_manifest'] = None
        self.assertEqual(compose_visibility(data)['coverage']['status'], 'partial')

    def test_rejected_paths_links_and_extensions_keep_uncertainty(self):
        for value in (member('../a'), member('/a'), member('a\\b'), member('x', kind='32'),
                      member('x', kind='78'), member('x', status='name-rejected')):
            with self.subTest(value=value):
                result = compose_visibility(inventory(layer(0, ['a']), layer(1, [value])))
                self.assertEqual(result['coverage']['status'], 'partial')
                self.assertEqual(rows(result)[0, 'a']['visibility'], 'uncertain')

    def test_nonempty_or_invalid_whiteout_not_applied(self):
        for value in (member('.wh.a'), member('.wh.', content=b''),
                      member('.wh.a', kind='35', content=b'')):
            result = compose_visibility(inventory(layer(0, ['a']), layer(1, [value])))
            self.assertEqual(rows(result)[0, 'a']['visibility'], 'uncertain')
            self.assertEqual(result['coverage']['status'], 'partial')

    def test_same_layer_file_parent_conflict_is_not_order_dependent(self):
        for names in (['a', 'a/b'], ['a/b', 'a']):
            result = compose_visibility(inventory(layer(0, names)))
            self.assertEqual(result['coverage']['status'], 'partial')
            self.assertTrue(all(r['visibility']=='uncertain' for r in result['entries']))

    def test_visible_java_candidates_only_with_nested_provenance(self):
        original = self.actual_java_inventory('a.jar', nested=True)
        before = copy.deepcopy(original)
        result = compose_visibility(original)
        candidate, = result['java_candidates']
        self.assertEqual(candidate['entry_chain'], [0])
        self.assertEqual(candidate['realm'], 'unknown')
        candidate['coordinates']['version'] = 'changed'
        self.assertEqual(original, before)
        original['layers'].append(layer(1, ['.wh.a.jar']))
        removed = compose_visibility(original)
        self.assertEqual(removed['java_candidates'], [])

    def test_unrecognized_top_issue_makes_all_results_uncertain(self):
        data=inventory(layer(0,['a']))
        data['issues'].append({'code':'source-changed'})
        self.assertEqual(compose_visibility(data)['entries'][0]['visibility'],'uncertain')

    def test_bounded_inputs_are_rejected_before_copy(self):
        for data, limits in ((inventory(layer(0,['x'*10000])), VisibilityLimits(max_text=16)),
                             (inventory(layer(0,['a','b'])), VisibilityLimits(max_members=1))):
            with self.assertRaises(VisibilityError):
                compose_visibility(data, limits=limits)

    def test_invalid_order_indices_and_scalar_aliases_rejected(self):
        for change in ('order','index','digest','size'):
            data=inventory(layer(0,['a']),layer(1,['b']))
            if change=='order': data['layers'].reverse()
            if change=='index': data['layers'][0]['distribution']['members'][0]['index']=True
            if change=='digest': data['layers'][0]['distribution']['members'][0]['sha256']={}
            if change=='size': data['layers'][0]['distribution']['members'][0]['size_actual']=[]
            with self.subTest(change=change),self.assertRaises(VisibilityError):
                compose_visibility(data)

    def test_work_budget_bounds_overlap_comparisons(self):
        with self.assertRaises(VisibilityError):
            compose_visibility(inventory(layer(0,['a','b']),layer(1,['.wh.a'])),
                               limits=VisibilityLimits(max_comparisons=1))

    def actual_java_inventory(self, name='app/lib.jar', nested=False, empty=False, directory=False):
        import io
        import tarfile
        import zipfile
        from tests.test_oci_layout import OCILayoutTests, TAR
        fixture = OCILayoutTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        jar = io.BytesIO()
        with zipfile.ZipFile(jar, 'w') as archive:
            if directory:
                archive.writestr('META-INF/', b'')
            if not empty:
                archive.writestr('META-INF/maven/g/a/pom.properties',
                                 'groupId=g\nartifactId=a\nversion=1\n')
        if nested:
            outer = io.BytesIO()
            with zipfile.ZipFile(outer, 'w') as archive:
                archive.writestr('lib/inner.jar', jar.getvalue())
            jar = outer
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            info = tarfile.TarInfo(name)
            info.size = len(jar.getvalue())
            archive.addfile(info, io.BytesIO(jar.getvalue()))
        fixture.index([fixture.image([fixture.blob(TAR, payload.getvalue())])])
        return fixture.inspect()

    def test_reserved_whiteout_ancestor_never_visible(self):
        for name in ('.wh.hidden/a.jar', 'app/.wh.hidden/a.jar',
                     'app/.wh..wh..opq/a.jar'):
            with self.subTest(name=name):
                result = compose_visibility(self.actual_java_inventory(name))
                self.assertEqual(result['coverage']['status'], 'partial')
                self.assertEqual(result['java_candidates'], [])
                self.assertTrue(all(row['visibility'] == 'uncertain' for row in result['entries']))

    def test_nested_archive_identity_must_match_enclosing_member(self):
        original = self.actual_java_inventory(nested=True)
        mutations = (
            ('sha256', '0' * 64), ('size', 999), ('inspection_status', 'rejected'),
            ('archive_id', None), ('entry_chain', ['invalid']), ('sha256', None),
        )
        for target in (0, 1):
            for key, value in mutations:
                with self.subTest(target=target, key=key, value=value):
                    data = copy.deepcopy(original)
                    data['layers'][0]['distribution']['archives'][0]['inspection']['archives'][target][key] = value
                    result = compose_visibility(data)
                    self.assertEqual(result['coverage']['status'], 'partial')
                    self.assertEqual(result['java_candidates'], [])

    def test_incomplete_and_empty_java_inventories_remain_honest(self):
        original = self.actual_java_inventory()
        for key, value in (('status', 'partial'), ('outer_capture', {'complete': False})):
            data = copy.deepcopy(original)
            data['layers'][0]['distribution']['archives'][0]['inspection']['coverage'][key] = value
            result = compose_visibility(data)
            self.assertEqual(result['coverage']['status'], 'partial')
            self.assertEqual(result['java_candidates'], [])
        data = copy.deepcopy(original)
        data['layers'][0]['distribution']['archives'][0]['inspection']['archives'] = []
        self.assertEqual(compose_visibility(data)['coverage']['status'], 'partial')

    def test_actual_jar_directory_metadata_preserves_visible_candidate(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                data = self.actual_java_inventory(directory=True, nested=nested)
                inspection = data['layers'][0]['distribution']['archives'][0]['inspection']
                directory, = [row for row in inspection['entries'] if row['kind'] == 'directory']
                self.assertEqual(directory['status'], 'recorded')
                self.assertEqual(directory['size_actual'], 0)
                self.assertIsNone(directory['sha256'])
                result = compose_visibility(data)
                self.assertEqual(result['coverage']['status'], 'complete-within-recorded-layers')
                self.assertEqual(len(result['java_candidates']), 1)
                self.assertEqual(result['java_candidates'][0]['coordinates']['artifactId'], 'a')

    def test_regular_recorded_entry_without_digest_is_not_directory_metadata(self):
        data = self.actual_java_inventory(directory=True)
        inspection = data['layers'][0]['distribution']['archives'][0]['inspection']
        regular = next(row for row in inspection['entries'] if row['kind'] == 'file')
        regular.update(status='recorded', sha256=None, size_actual=0, size_declared=0)
        result = compose_visibility(data)
        self.assertEqual(result['coverage']['status'], 'partial')
        self.assertEqual(result['java_candidates'], [])

    def test_invalid_directory_metadata_is_not_treated_as_content_free(self):
        original = self.actual_java_inventory(directory=True)
        for update in ({'size_actual': 1, 'size_declared': 1}, {'is_archive': True},
                       {'sha256': '0' * 64}, {'kind': 'unknown'}, {'name': 'META-INF'}):
            with self.subTest(update=update):
                data = copy.deepcopy(original)
                directory = next(row for row in data['layers'][0]['distribution']['archives'][0]
                                 ['inspection']['entries'] if row['kind'] == 'directory')
                directory.update(update)
                result = compose_visibility(data)
                self.assertEqual(result['coverage']['status'], 'partial')
                self.assertEqual(result['java_candidates'], [])

    def test_actual_empty_jar_remains_complete_without_candidates(self):
        result = compose_visibility(self.actual_java_inventory(empty=True))
        self.assertEqual(result['coverage']['status'], 'complete-within-recorded-layers')
        self.assertEqual(result['entries'][0]['visibility'], 'visible')
        self.assertEqual(result['java_candidates'], [])

    def test_actual_reader_inventory_whiteouts(self):
        from tests.test_oci_layout import OCILayoutTests, TAR, tar_bytes
        fixture = OCILayoutTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        first = fixture.blob(TAR, tar_bytes(('lib/old.txt', 'keep.txt')))
        second = fixture.blob(TAR, tar_bytes(('lib/.wh..wh..opq', 'lib/new.txt')))
        fixture.index([fixture.image([first, second])])
        observed = fixture.inspect()
        self.assertEqual(len(observed['layers']), 2)
        result = compose_visibility(observed)
        self.assertEqual(result['coverage']['status'], 'complete-within-recorded-layers')
        data = rows(result)
        self.assertEqual(data[0, 'lib/old.txt']['visibility'], 'removed')
        self.assertEqual(data[0, 'keep.txt']['visibility'], 'visible')
        self.assertEqual(data[1, 'lib/new.txt']['visibility'], 'visible')

    def test_limits_path_depth_and_output_size(self):
        with self.assertRaises(VisibilityError):
            compose_visibility(inventory(layer(0, ['a/b/c'])),
                               limits=VisibilityLimits(max_path_depth=2))
        with self.assertRaises(VisibilityError):
            compose_visibility(inventory(layer(0, ['a'])),
                               limits=VisibilityLimits(max_output_bytes=100))

    def test_empty_selected_manifest_is_valid_empty_recorded_view(self):
        result=compose_visibility(inventory())
        self.assertEqual(result['entries'],[])
        self.assertEqual(result['coverage']['status'],'complete-within-recorded-layers')
        self.assertFalse(result['coverage']['definitive_absence'])


if __name__=='__main__':
    unittest.main()
