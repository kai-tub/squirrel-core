from __future__ import annotations

import os
import tempfile
import typing as t
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import dask.distributed
import mlflow
import numpy as np
import pytest
import wandb

from squirrel.iterstream import Composable, FilePathGenerator, IterableSamplerSource, IterableSource
from squirrel.iterstream.iterators import take_
from squirrel.iterstream.metrics import MetricsConf

if t.TYPE_CHECKING:
    from squirrel.constants import SampleType


def test_iterablesource() -> None:
    """Test instantiating IterableSource in different ways"""

    def _gen() -> t.Any:
        """A generator without argument"""
        for i in range(3):
            yield i

    def _gen_2(num: int) -> t.Any:
        """A generator with arguments"""
        for i in range(num):
            yield i

    class _Gen:
        def __init__(self, items: t.List):
            self.items = items

        def __iter__(self):
            yield from self.items

    s = [0, 1, 2]
    it1 = IterableSource(range(3)).collect()
    it2 = IterableSource(s).collect()
    it3 = IterableSource(_gen).collect()
    it4 = IterableSource(_gen()).collect()
    it5 = IterableSource(partial(_Gen, s)).collect()
    it6 = IterableSource(partial(_gen_2, 3)).collect()
    it7 = IterableSource(_Gen(s)).collect()
    assert s == it1 == it2 == it3 == it4 == it5 == it6 == it7


def test_map(samples: t.List[t.Dict]) -> None:
    """Test map"""
    res_1 = IterableSource(samples).map(lambda sample: _f(sample, 3)).map(lambda sample: sample["label"]).collect()

    assert all(i == 3 for i in res_1)


def test_compose() -> None:
    """Test composing without init method"""

    class Add1(Composable):
        def __iter__(self):
            for i in self.source:
                yield i + 1

    lis = [1, 2, 3]
    it = IterableSource(lis).compose(Add1).collect()
    assert it == [2, 3, 4]


def test_async_map(samples: t.List[SampleType]) -> None:
    """Test async_map"""
    res = (
        IterableSource(samples)
        .async_map(lambda sample: _f(sample, 4))
        .async_map(lambda sample: sample["label"])
        .collect()
    )
    assert all(i == 4 for i in res)


def test_filter(samples: t.List[SampleType]) -> None:
    """Test filter"""
    res = IterableSource(samples).filter(lambda s: s["label"] == 3).collect()
    assert len(res) == 0


def test_take(samples: t.List[SampleType]) -> None:
    """Test take"""
    # take less than elements in iterator
    res = IterableSource(samples).take(len(samples) - 1).collect()
    assert len(res) == len(samples) - 1

    # take more than elements in iterator
    res = IterableSource(samples).take(len(samples) + 1).collect()
    assert len(res) == len(samples)

    # take all elements in iterator
    res = IterableSource(samples).take(len(samples)).collect()
    assert len(res) == len(samples)


def test_take_exact(samples: t.List[SampleType]) -> None:
    """Test take_exact"""

    # take all elements in iterator
    it = IterableSource(samples).loop().take(len(samples)).collect()
    assert len(it) == len(samples)

    for n in np.random.choice(range(0, 100), 10, replace=False):
        it = IterableSource(samples).loop().take(n).collect()
        assert len(it) == n

    # take 0 elements
    it = IterableSource(samples).loop().take(0).collect()
    assert len(it) == 0

    # ensure that empty iterable works as expected
    it = IterableSource([]).loop().take(2).collect()
    assert len(it) == 0


@pytest.mark.parametrize("n", [0, 2, 4])
def test_loop(samples: t.List[SampleType], n: int) -> None:
    """Test loop"""
    it = IterableSource(samples).loop(n).collect()
    assert len(it) == n * len(samples)
    assert IterableSource([1, 2, 3]).loop(3).collect() == [1, 2, 3, 1, 2, 3, 1, 2, 3]


def test_take_side_effect() -> None:
    """Test that take_ fetches correct number of elements from an iterator."""
    lst = [1, 2, 3, 4]
    it = iter(lst)
    assert list(take_(it, 2)) == [1, 2]
    assert list(take_(it, 2)) == [3, 4]


def test_take_less_elements() -> None:
    """Check that trying to take more elements than possible does not lead to errors."""
    assert list(take_([1, 2, 3], 10)) == [1, 2, 3]


def test_batched(samples: t.List[SampleType]) -> None:
    """Test batched with and without dropping non-full last batch"""
    res_drop = IterableSource(samples).batched(3, drop_last_if_not_full=True).collect()
    res_no_drop = IterableSource(samples).batched(3, drop_last_if_not_full=False).collect()
    assert len(res_drop) == 3
    assert len(res_no_drop) == 4
    assert all(len(batch) == 3 for batch in res_drop)


def test_shuffle(samples: t.List[SampleType]) -> None:
    """Test shuffle"""
    ids = [s["key"] for s in samples]
    res_shuffled = IterableSource(samples).shuffle(10).map(lambda x: x["key"]).collect()
    assert len(set(ids) - set(res_shuffled)) == 0
    assert ids != res_shuffled
    assert sorted(ids) == sorted(res_shuffled)


def test_async_map_executor() -> None:
    """Test passing an executor to async_map"""
    exec_ = ThreadPoolExecutor(max_workers=2)
    res_1 = IterableSource(range(10)).async_map(lambda x: x + 1, executor=exec_).collect()
    # pass it to another stream to make sure it's not closed by squirrel when IterableSource is exhausted
    res_2 = (
        IterableSource(range(10))
        .async_map(lambda x: x + 2, executor=exec_)
        .async_map(lambda x: x - 1, executor=exec_)
        .collect()
    )
    exec_.shutdown()
    assert [i + 1 for i in range(10)] == res_1
    assert res_1 == res_2


def test_different_maps() -> None:
    """Test mapping a value with map, async_map, dask_map, and numba_map"""

    def _add_one(x: int) -> int:
        return x + 1

    items = list(range(10))
    res_1 = IterableSource(items).dask_map(_add_one).materialize_dask().collect()
    res_2 = IterableSource(items).numba_map(_add_one).collect()
    res_3 = IterableSource(items).map(_add_one).collect()
    res_4 = IterableSource(items).async_map(_add_one).collect()
    assert res_1 == res_2 == res_3 == res_4

    res_5 = IterableSource(items).dask_map(_add_one).map(_add_one).numba_map(_add_one).materialize_dask().collect()
    res_6 = IterableSource(items).map(_add_one).map(_add_one).map(_add_one).collect()
    assert res_5 == res_6


def test_dask(samples: t.List[SampleType]) -> None:
    """Test async_map with dask executor"""
    client = dask.distributed.Client()
    res = IterableSource([1, 2, 3]).async_map(lambda x: x**2, executor=client).collect()
    client.shutdown()
    assert res == [1, 4, 9]


def test_tqdm(samples: t.List[SampleType]) -> None:
    """Smoke test tqdm shorthand"""
    IterableSource(samples).tqdm().join()


@pytest.mark.skip(reason="Wandb asks for a user token. Skip until we have set up a bot account token.")
@pytest.mark.parametrize("metrics_conf_iops", [True, False])
@pytest.mark.parametrize("metrics_conf_throughput", [True, False])
@pytest.mark.parametrize("multi_points", [1, 2])
def test_metrics_tracking_with_wandb(
    toggle_wandb: None,
    metrics_conf_iops: bool,
    metrics_conf_throughput: bool,
    create_all_iterable_source: Composable,
    multi_points: int,
) -> None:
    """Smoke test for tracked iterable source, when callback set to be wandb.log."""
    conf = MetricsConf(iops=metrics_conf_iops, throughput=metrics_conf_throughput)
    it = create_all_iterable_source
    if multi_points == 1:
        with wandb.init("squirrel_test"):
            it.monitor(callback=wandb.log, metrics_conf=conf).collect()
    elif multi_points == 2:
        with wandb.init("squirrel-test"):
            (
                it.monitor(wandb.log, prefix="(before shuffle) ", metrics_conf=conf)
                .shuffle(20)
                .monitor(wandb.log, prefix="(after shuffle) ", metrics_conf=conf)
                .collect()
            )


@pytest.mark.parametrize("metrics_conf_iops", [True, False])
@pytest.mark.parametrize("metrics_conf_throughput", [True, False])
@pytest.mark.parametrize("multi_points", [1, 2])
def test_metrics_tracking_with_mlflow(
    metrics_conf_iops: bool, metrics_conf_throughput: bool, create_all_iterable_source: Composable, multi_points: int
) -> None:
    """Smoke test for tracked iterable source, when callback set to be mlflow.log_metrics."""
    conf = MetricsConf(iops=metrics_conf_iops, throughput=metrics_conf_throughput)
    it = create_all_iterable_source
    if multi_points == 1:
        with mlflow.start_run(run_name="squirrel-test"):
            it.monitor(mlflow.log_metrics, metrics_conf=conf).collect()
    elif multi_points == 2:
        with mlflow.start_run(run_name="squirrel-test"):
            (
                it.monitor(mlflow.log_metrics, prefix="before shuffle ", metrics_conf=conf)
                .shuffle(20)
                .monitor(mlflow.log_metrics, prefix="after shuffle ", metrics_conf=conf)
                .collect()
            )


@pytest.mark.parametrize("probs", [[0.4, 0.6], None])
def test_iterablesamplersource_all_sampled(probs: t.Optional[t.List[float]]) -> None:
    """Smoke test IterableSamplerSource"""
    res_1 = IterableSource([0, 1, 2, 3])
    res_2 = IterableSource([4, 5, 6])
    res = IterableSamplerSource([res_1, res_2], probs=probs).collect()
    assert set(res) == set(range(7))


def test_filepathgenerator_nested() -> None:
    """Test FilePathGenerator with on without nested argument"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        for d in range(2):
            for sub in range(2):
                basedir = f"{tmp_dir}/{d}"
                if not os.path.exists(basedir):
                    os.makedirs(basedir)
                with open(f"{tmp_dir}/{d}/{sub}.csv", mode="x") as f:
                    f.write("")

        dirs = FilePathGenerator(url=tmp_dir).collect()
        files = FilePathGenerator(url=tmp_dir, nested=True).collect()
    assert len(dirs) == 2
    assert len(files) == 4


@pytest.fixture
def samples() -> t.List[SampleType]:
    """A fixture to get a list of samples"""
    return [get_sample() for _ in range(10)]


def get_sample() -> SampleType:
    """Return a single sample with random values"""
    return {
        "key": f"_{np.random.randint(1, 10000)}",
        "image": np.random.random(size=(1, 1, 1)),
        "label": np.random.choice([0, 1]),
        "meta": {"key": "value", "split": np.random.choice(["train", "test", "validation"])},
        "cache_spec": [
            "label",
        ],
    }


def _f(sample: SampleType, value: int) -> SampleType:
    sample["label"] = value
    return sample
