"""D3.32 protocol chunking regressions."""
import unittest
from unittest.mock import patch

from kbsys import db, structure, extract, objects
from . import test_core


class ProtocolChunkTests(unittest.TestCase):
    setUp = test_core.Demo3Tests.setUp
    tearDown = test_core.Demo3Tests.tearDown

    def write_methods(self, subsections):
        """Replace Methods rows while retaining the fixture's other sections."""
        rows = [row for row in self.rows if row['section'] != 'methods']
        seq = max(row['seq'] for row in rows) + 1
        methods = []
        for heading, count in subsections:
            for number in range(1, count + 1):
                methods.append({**self.rows[2],
                                'sentence_id': f'{self.pid}:M{len(methods) + 1:04d}',
                                'seq': seq,
                                'passage_id': f'{self.pid}:methods-{len(methods) + 1:04d}',
                                'subsection': heading,
                                'text': f'{heading} step {number} used the source method detail.'})
                seq += 1
        rows.extend(methods)
        rows.sort(key=lambda row: row['seq'])
        self.rows = rows
        db.write_jsonl(self.folder / 'sentences.jsonl', rows)
        return methods

    def test_tiny_subsections_merge_and_headings_are_reference_labels(self):
        methods = self.write_methods([('Preparation', 3), ('Exposure', 3), ('Readout', 3)])
        task = structure.prepare_structured(self.pid, 'protocol', persist=True)
        state = db.read_json(structure._task_path(self.pid, task['task_id']))

        self.assertEqual(task['total_chunks'], 1)
        self.assertEqual(state['chunks'][0]['sentence_ids'], [row['sentence_id'] for row in methods])
        self.assertEqual(state['chunks'][0]['subsection_headings'], ['Preparation', 'Exposure', 'Readout'])
        self.assertEqual(task['subsection_headings'], ['Preparation', 'Exposure', 'Readout'])
        self.assertEqual(task['chunk']['sentence_ids'], [row['sentence_id'] for row in methods])

    def test_large_subsection_splits_only_at_sentence_boundaries(self):
        methods = self.write_methods([('Long technique', 7)])
        with patch.object(structure, 'CHUNK_SENTENCES', 3):
            task = structure.prepare_structured(self.pid, 'protocol', persist=True)
        state = db.read_json(structure._task_path(self.pid, task['task_id']))
        chunks = state['chunks']

        self.assertEqual([len(chunk['sentence_ids']) for chunk in chunks], [3, 3, 1])
        self.assertEqual([sid for chunk in chunks for sid in chunk['sentence_ids']],
                         [row['sentence_id'] for row in methods])
        self.assertTrue(all(chunk['subsection_headings'] == ['Long technique'] for chunk in chunks))

    def test_wh_chunking_remains_subsection_based(self):
        rows = [row for row in self.rows if row['section'] != 'introduction']
        intro = []
        for number, heading in enumerate(('Question A', 'Question B'), 1):
            intro.append({**self.rows[number - 1],
                          'sentence_id': f'{self.pid}:I{number:04d}',
                          'seq': number,
                          'passage_id': f'{self.pid}:intro-{number:04d}',
                          'section': 'introduction',
                          'subsection': heading,
                          'text': f'{heading} is stated in the Introduction.'})
        rows.extend(intro)
        rows.sort(key=lambda row: row['seq'])
        db.write_jsonl(self.folder / 'sentences.jsonl', rows)

        task = structure.prepare_structured(self.pid, 'wh', persist=False)

        self.assertEqual(task['total_chunks'], 2)
        self.assertEqual(task['chunk']['sentence_ids'], ['test2026_example_abcd:I0001'])

    def test_protocol_ids_stay_in_source_order(self):
        methods = self.write_methods([('First', 8), ('Second', 8), ('Third', 8)])
        task = structure.prepare_structured(self.pid, 'protocol', persist=True)
        state = db.read_json(structure._task_path(self.pid, task['task_id']))
        expected = [row['sentence_id'] for row in methods]
        actual = [sid for chunk in state['chunks'] for sid in chunk['sentence_ids']]

        self.assertEqual(actual, expected)
        self.assertEqual([heading for chunk in state['chunks'] for heading in chunk['subsection_headings']],
                         ['First', 'Second', 'Third'])


def row(sid, text, *, seq, passage=None, kind="text", section="results", **extra):
    return {"sentence_id": sid, "seq": seq, "passage_id": passage or f"p:{seq:04d}",
            "source": "main", "page": 1, "section": section, "kind": kind,
            "text": text, "citations": [], "mentions": [], **extra}


class ChunkContinuationTests(unittest.TestCase):
    def test_chunks_are_atomic_over_continuation_targets(self):
        rows = [row("p:S1", "first fragment", seq=1, subsection="A"),
                row("p:S2", "second fragment.", seq=2, subsection="A"),
                row("p:S3", "independent sentence.", seq=3, subsection="A")]
        rows[0]["continues"] = "p:S2"
        with patch.object(structure, "CHUNK_SENTENCES", 1):
            chunks = structure._chunks(rows)
        self.assertEqual(chunks[0], ["p:S1", "p:S2"])
        self.assertNotIn("p:S2", chunks[1:])

    def test_new_import_sentence_end_preserves_superscript_citation(self):
        parts = extract._split_long("First sentence.^128 Next sentence continues here.", 24)
        self.assertTrue(any(".^128" in part for part in parts))

    def test_wrapped_table_cell_lines_merge_into_one_row(self):
        rect = objects.pymupdf.Rect
        words = [
            (rect(1, 20, 25, 28), "Control"), (rect(40, 20, 50, 28), "10"),
            (rect(60, 20, 70, 28), "20"), (rect(40, 30, 68, 38), "continued"),
            (rect(1, 42, 25, 50), "Treated"), (rect(40, 42, 50, 50), "11"),
            (rect(60, 42, 70, 50), "21"),
        ]
        fake_page = type("Page", (), {"rect": rect(0, 0, 100, 200)})()
        with patch.object(objects, "_long_blocks", return_value=[]), patch.object(objects, "_words", return_value=words):
            header, rows, _, _ = objects._from_words(fake_page, rect(0, 0, 100, 10), [])
        self.assertEqual(len(rows), 2)
        self.assertIn("continued", " ".join(rows[0]))


if __name__ == '__main__':
    unittest.main()
