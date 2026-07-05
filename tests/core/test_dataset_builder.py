"""Tests for dataset splitting and (de)serialization."""

import pytest

from evolution.core.dataset_builder import EvalDataset, EvalExample, split_examples


def _examples(n):
    return [
        EvalExample(task_input=f"task {i}", expected_behavior=f"behavior {i}")
        for i in range(n)
    ]


class TestSplitExamples:
    def test_standard_50_25_25(self):
        ds = split_examples(_examples(20))
        assert len(ds.train) == 10
        assert len(ds.val) == 5
        assert len(ds.holdout) == 5

    def test_no_split_empty_for_three_or_more(self):
        for n in range(3, 12):
            ds = split_examples(_examples(n))
            assert len(ds.train) >= 1, f"empty train for n={n}"
            assert len(ds.val) >= 1, f"empty val for n={n}"
            assert len(ds.holdout) >= 1, f"empty holdout for n={n}"
            assert len(ds.all_examples) == n

    def test_two_examples_prioritizes_train_and_holdout(self):
        ds = split_examples(_examples(2))
        assert len(ds.train) == 1
        assert len(ds.holdout) == 1
        assert len(ds.val) == 0

    def test_single_example_goes_to_train(self):
        ds = split_examples(_examples(1))
        assert len(ds.train) == 1
        assert len(ds.all_examples) == 1

    def test_empty_input(self):
        ds = split_examples([])
        assert ds.all_examples == []

    def test_no_examples_lost_or_duplicated(self):
        ds = split_examples(_examples(17))
        inputs = [e.task_input for e in ds.all_examples]
        assert sorted(inputs) == sorted(e.task_input for e in _examples(17))

    def test_seed_makes_split_deterministic(self):
        a = split_examples(_examples(10), seed=42)
        b = split_examples(_examples(10), seed=42)
        assert [e.task_input for e in a.train] == [e.task_input for e in b.train]
        assert [e.task_input for e in a.holdout] == [e.task_input for e in b.holdout]


class TestEvalDatasetRoundtrip:
    def test_save_and_load(self, tmp_path):
        ds = split_examples(_examples(8))
        ds.save(tmp_path)
        loaded = EvalDataset.load(tmp_path)
        assert len(loaded.train) == len(ds.train)
        assert len(loaded.val) == len(ds.val)
        assert len(loaded.holdout) == len(ds.holdout)

    def test_to_dspy_examples(self):
        ds = split_examples(_examples(4))
        dspy_examples = ds.to_dspy_examples("train")
        assert len(dspy_examples) == len(ds.train)
        assert dspy_examples[0].task_input.startswith("task")
