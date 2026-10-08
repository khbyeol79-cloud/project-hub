from contextlib import closing
import unittest
from unittest.mock import patch

import test_file_search as fixtures
from bot import database, file_search as search


class VersionFixture(fixtures.CatalogueFixture):
    def add_version(self, digest, *, filename="도면.pdf", day=1, **kwargs):
        file_id = self.add_file(filename, date=f"2026-10-{day:02d}T12:00:00+09:00", **kwargs)
        with closing(database.get_connection()) as conn, conn:
            conn.execute("UPDATE files SET sha256=?, version=999, version_group='unrelated-global-group' WHERE id=?",
                         (digest, file_id))
        return file_id


class VersionDatabaseTests(VersionFixture, unittest.TestCase):
    def test_changed_content_and_reuploads_form_history(self):
        self.add_version("a", day=1)
        self.add_version("a", day=2)
        self.add_version("b", day=3)
        self.add_version("b", day=4)
        rows, total = search.find_versions(1, [10], "도면")
        self.assertEqual(total, 2)
        self.assertEqual([r['version_number'] for r in rows], [2, 1])
        self.assertEqual([r['version_total'] for r in rows], [2, 2])
        self.assertEqual([r['upload_count'] for r in rows], [2, 2])
        self.assertTrue(rows[0]['uploaded_at'].startswith('2026-10-04'))
        self.assertNotIn('local_path', rows[0])
        self.assertNotIn('sha256', rows[0])

    def test_return_to_old_content_is_a_new_revision(self):
        for day, digest in enumerate(['a', 'b', 'a', 'a'], 1):
            self.add_version(digest, day=day)
        rows, total = search.find_versions(1, [10], '도면')
        self.assertEqual(total, 3)
        self.assertEqual([r['version_number'] for r in rows], [3, 2, 1])
        self.assertEqual([r['upload_count'] for r in rows], [2, 1, 1])

    def test_unrelated_channels_categories_and_filenames_remain_separate(self):
        self.add_version('a')
        self.add_version('b', channel='20')
        self.add_version('c', category='plc')
        self.add_version('a', filename='도면-다른작업.pdf')
        rows, total = search.find_versions(1, [10, 20], '도면')
        self.assertEqual(total, 4)
        self.assertTrue(all(r['version_number'] == r['version_total'] == 1 for r in rows))
        self.assertEqual(search.find_versions(1, [10, 20], '도면', 'plc')[1], 1)

    def test_hidden_records_cannot_change_numbers_counts_or_links(self):
        self.add_version('a', drive_id='visible-id')
        for day in range(1, 9):
            self.add_version(str(day), day=day, channel='20', drive_id='secret-id')
            self.add_version(str(day), day=day, guild='2', drive_id='other-server-id')
        rows, total = search.find_versions(1, [10], '도면')
        self.assertEqual(total, 1)
        self.assertEqual((rows[0]['version_number'], rows[0]['version_total'], rows[0]['upload_count']), (1, 1, 1))
        self.assertEqual(rows[0]['google_drive_file_id'], 'visible-id')
        self.assertEqual(search.find_versions(1, [], '도면'), ([], 0))

    def test_case_and_unicode_variations_match_same_file(self):
        self.add_version('a', filename='회의.PDF', day=1)
        self.add_version('b', filename='회의.pdf', day=2)
        rows, total = search.find_versions(1, [10], '회의.PDF')
        self.assertEqual(total, 2)
        self.assertEqual([r['version_number'] for r in rows], [2, 1])
        self.assertEqual([r['version_total'] for r in rows], [2, 2])

    def test_backfilled_history_uses_message_date_not_insert_order(self):
        self.add_version('new', day=3)
        self.add_version('old', day=1)
        self.add_version('old', day=2)
        rows, total = search.find_versions(1, [10], '도면')
        self.assertEqual(total, 2)
        self.assertEqual([r['version_number'] for r in rows], [2, 1])
        self.assertEqual([r['upload_count'] for r in rows], [1, 2])

    def test_completed_link_for_same_version_survives_pending_reupload(self):
        self.add_version('a', day=1, drive_id='version-a')
        self.add_version('b', day=2, drive_id='version-b')
        self.add_version('b', day=3, drive_id=None, status='pending')
        rows, _ = search.find_versions(1, [10], '도면')
        self.assertEqual(rows[0]['google_drive_file_id'], 'version-b')
        self.assertEqual(rows[0]['upload_status'], 'uploaded')
        self.assertEqual(rows[0]['upload_count'], 2)
        self.assertTrue(rows[0]['uploaded_at'].startswith('2026-10-03'))

    def test_previous_version_link_is_not_used_for_unuploaded_new_content(self):
        self.add_version('a', day=1, drive_id='old-version')
        self.add_version('b', day=2, drive_id=None, status='failed')
        rows, _ = search.find_versions(1, [10], '도면')
        self.assertIsNone(rows[0]['google_drive_file_id'])
        self.assertEqual(rows[0]['upload_status'], 'failed')

    def test_version_paging_does_not_split_duplicate_uploads(self):
        for day in range(1, 8):
            self.add_version(str(day), day=day)
            self.add_version(str(day), day=day)
        first, total = search.find_versions(1, [10], '도면')
        second, _ = search.find_versions(1, [10], '도면', page=2)
        self.assertEqual(total, 7)
        self.assertEqual([r['version_number'] for r in first], [7, 6, 5, 4, 3])
        self.assertEqual([r['version_number'] for r in second], [2, 1])
        self.assertTrue(all(r['upload_count'] == 2 for r in first + second))
        self.assertEqual(search.find_versions(1, [10], '도면', page=3), ([], 7))

    def test_unknown_hash_is_not_assumed_same_content(self):
        self.add_version('', day=1)
        self.add_version('', day=2)
        self.assertEqual(search.find_versions(1, [10], '도면')[1], 2)

    def test_literals_and_input_validation(self):
        self.add_version('a', filename='도면_100%.pdf')
        self.assertEqual(search.find_versions(1, [10], '100%')[1], 1)
        self.assertEqual(search.find_versions(1, [10], "' OR 1=1 --")[1], 0)
        for keyword, kwargs in [(' ', {}), ('a' * 101, {}), ('도면', {'page': 0}),
                                ('도면', {'page': 10001}), ('도면', {'category': 'invalid'})]:
            with self.assertRaises(ValueError):
                search.find_versions(1, [10], keyword, **kwargs)

    def test_embed_marks_latest_previous_dates_links_and_reuploads(self):
        self.add_version('a', day=1, drive_id='old-link')
        self.add_version('b', day=2, drive_id='latest-link')
        self.add_version('b', day=3, drive_id='latest-copy')
        rows, total = search.find_versions(1, [10], '도면')
        embed = search.result_embed(rows, total, '도면', versions=True).to_dict()
        self.assertEqual(embed['title'], '파일 버전')
        self.assertIn('[최신 V2/2]', embed['fields'][0]['name'])
        self.assertIn('[이전 V1/2]', embed['fields'][1]['name'])
        self.assertIn('동일 내용 2회', embed['fields'][0]['value'])
        self.assertIn('2026-10-03 12:00', embed['fields'][0]['value'])
        self.assertIn('file/d/latest-copy/view', embed['fields'][0]['value'])
        self.assertIn('file/d/old-link/view', embed['fields'][1]['value'])
        self.assertIn('2개 버전', embed['footer']['text'])


class VersionCommandTests(VersionFixture, unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.CommandTests.asyncSetUp
    interaction = fixtures.CommandTests.interaction

    async def test_command_runs_privately_with_fresh_visibility_check(self):
        self.add_version('a', day=1)
        self.add_version('b', day=2)
        self.add_version('secret', day=3, channel='20', drive_id='private-link')
        interaction = self.interaction()
        channels = {10: fixtures.channel(), 20: fixtures.channel(20, visible=False)}
        with patch.object(self.client, 'get_channel', side_effect=channels.get):
            await self.commands.tree.get_command('버전').callback(interaction, '도면')
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        embed = interaction.edit_original_response.call_args.kwargs['embed'].to_dict()
        self.assertEqual(len(embed['fields']), 2)
        self.assertIn('[최신 V2/2]', embed['fields'][0]['name'])
        self.assertNotIn('private-link', str(embed))
        self.assertFalse(interaction.edit_original_response.call_args.kwargs['allowed_mentions'].everyone)

    async def test_command_schema_and_blank_input(self):
        command = self.commands.tree.get_command('버전')
        self.assertTrue(command.guild_only)
        self.assertEqual([p['name'] for p in command.to_dict(self.commands.tree)['options']],
                         ['키워드', '분류', '페이지', '자료범위'])
        interaction = self.interaction()
        await command.callback(interaction, '   ')
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(interaction.response.send_message.call_args.kwargs['ephemeral'])
        interaction.response.defer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
