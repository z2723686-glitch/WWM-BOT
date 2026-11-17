# -*- coding: utf-8 -*-
"""
In-memory stand-in for the Motor/MongoDB calls the reward system uses.

It reproduces the semantics the reward code RELIES ON: unique indexes
(DuplicateKeyError), conditional atomic updates ($ne/$gt/$gte/... filters with
$inc/$set/$push/$setOnInsert), upserts and find_one_and_update. Every operation
yields to the event loop first (so concurrent tasks really interleave) and is then
atomic - like a single-document MongoDB operation.

It is NOT a MongoDB: it proves the application logic, not the server. Use a
disposable real MongoDB (see docs) for the final check before production.
"""

import asyncio
import copy
import uuid

from pymongo.errors import DuplicateKeyError, OperationFailure

MISSING = object()


def _eq(val, cond):
    if val is MISSING:
        return cond is None
    if isinstance(val, list) and not isinstance(cond, list):
        return cond in val
    return val == cond


def _op(op, val, arg):
    if op == "$eq":
        return _eq(val, arg)
    if op == "$ne":
        return not _eq(val, arg)
    if op in ("$gt", "$gte", "$lt", "$lte"):
        if val is MISSING or val is None:
            return False
        return {"$gt": val > arg, "$gte": val >= arg, "$lt": val < arg, "$lte": val <= arg}[op]
    if op == "$in":
        return any(_eq(val, a) for a in arg)
    if op == "$nin":
        return not any(_eq(val, a) for a in arg)
    if op == "$exists":
        return (val is not MISSING) == bool(arg)
    raise NotImplementedError("fake mongo: operator " + op)


def _match(doc, flt):
    for key, cond in (flt or {}).items():
        assert "." not in key, "fake mongo: dotted paths not supported"
        val = doc.get(key, MISSING)
        if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
            if not all(_op(o, val, a) for o, a in cond.items()):
                return False
        elif not _eq(val, cond):
            return False
    return True


def _apply(doc, update, inserting):
    for op, fields in update.items():
        if op == "$set":
            for k, v in fields.items():
                doc[k] = copy.deepcopy(v)
        elif op == "$setOnInsert":
            if inserting:
                for k, v in fields.items():
                    doc[k] = copy.deepcopy(v)
        elif op == "$inc":
            for k, v in fields.items():
                doc[k] = doc.get(k, 0) + v
        elif op == "$push":
            for k, v in fields.items():
                arr = doc.setdefault(k, [])
                if isinstance(v, dict) and "$each" in v:
                    arr.extend(copy.deepcopy(v["$each"]))
                    if "$slice" in v and v["$slice"] < 0:
                        del arr[:max(0, len(arr) + v["$slice"])]
                else:
                    arr.append(copy.deepcopy(v))
        elif op == "$addToSet":
            for k, v in fields.items():
                arr = doc.setdefault(k, [])
                for item in (v["$each"] if isinstance(v, dict) and "$each" in v else [v]):
                    if item not in arr:
                        arr.append(item)
        elif op == "$pull":
            for k, v in fields.items():
                arr = doc.get(k, [])
                drop = v["$in"] if isinstance(v, dict) and "$in" in v else [v]
                doc[k] = [x for x in arr if x not in drop]
        elif op == "$unset":
            for k in fields:
                doc.pop(k, None)
        else:
            raise NotImplementedError("fake mongo: update operator " + op)


class UpdateResult:
    def __init__(self, matched, modified, upserted_id=None):
        self.matched_count, self.modified_count, self.upserted_id = matched, modified, upserted_id


class FakeCursor:
    def __init__(self, docs):
        self._docs, self._sort, self._skip, self._limit = docs, [], 0, 0

    def sort(self, key, direction=None):
        self._sort = list(key) if isinstance(key, list) else [(key, direction if direction is not None else 1)]
        return self

    def skip(self, n):
        self._skip = n
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _result(self):
        docs = list(self._docs)
        for key, direction in reversed(self._sort):
            docs.sort(key=lambda d, k=key: (d.get(k) is None, d.get(k)), reverse=direction < 0)
        docs = docs[self._skip:]
        if self._limit:
            docs = docs[:self._limit]
        return [copy.deepcopy(d) for d in docs]

    async def to_list(self, length=None):
        await asyncio.sleep(0)
        out = self._result()
        return out[:length] if length else out

    def __aiter__(self):
        async def gen():
            await asyncio.sleep(0)
            for d in self._result():
                yield d
        return gen()


class FakeCollection:
    def __init__(self, name):
        self.name, self.docs, self.indexes = name, [], {}

    # ── indexes ──
    async def create_indexes(self, models):
        await asyncio.sleep(0)
        for m in models:
            doc = m.document
            name, keys = doc.get("name"), [k for k, _ in doc["key"]]
            spec = (tuple(keys), bool(doc.get("unique")))
            if name in self.indexes and self.indexes[name] != spec:
                raise OperationFailure("Index with name {} already exists with different options".format(name))
            self.indexes[name] = spec
        return list(self.indexes)

    def _unique_check(self, candidate, ignore=None):
        for name, (keys, unique) in self.indexes.items():
            if not unique:
                continue
            ident = tuple(candidate.get(k) for k in keys)
            for other in self.docs:
                if other is ignore:
                    continue
                if tuple(other.get(k) for k in keys) == ident:
                    raise DuplicateKeyError("E11000 duplicate key error index: {} dup key: {}".format(name, ident))

    # ── writes ──
    async def insert_one(self, doc):
        await asyncio.sleep(0)
        doc = copy.deepcopy(doc)
        doc.setdefault("_id", uuid.uuid4().hex)
        self._unique_check(doc)
        self.docs.append(doc)
        return type("R", (), {"inserted_id": doc["_id"]})()

    def _find_first(self, flt):
        for d in self.docs:
            if _match(d, flt):
                return d
        return None

    async def _update(self, flt, update, upsert, many=False, return_doc=None):
        await asyncio.sleep(0)
        targets = [d for d in self.docs if _match(d, flt)]
        if not many:
            targets = targets[:1]
        if not targets:
            if not upsert:
                return UpdateResult(0, 0), None
            new = {k: copy.deepcopy(v) for k, v in flt.items()
                   if not (isinstance(v, dict) and v and all(x.startswith("$") for x in v))}
            _apply(new, update, True)
            new.setdefault("_id", uuid.uuid4().hex)
            self._unique_check(new)
            self.docs.append(new)
            return UpdateResult(0, 0, new["_id"]), (copy.deepcopy(new) if return_doc else None)
        modified, last = 0, None
        for d in targets:
            before = copy.deepcopy(d)
            work = copy.deepcopy(d)
            _apply(work, update, False)
            self._unique_check(work, ignore=d)
            d.clear()
            d.update(work)
            modified += 1 if work != before else 0
            last = (before, copy.deepcopy(d))
        return UpdateResult(len(targets), modified), last

    async def update_one(self, flt, update, upsert=False):
        res, _ = await self._update(flt, update, upsert)
        return res

    async def update_many(self, flt, update, upsert=False):
        res, _ = await self._update(flt, update, upsert, many=True)
        return res

    async def find_one_and_update(self, flt, update, return_document=False, upsert=False, **_):
        res, pair = await self._update(flt, update, upsert, return_doc=True)
        if pair is None:
            return None
        if isinstance(pair, dict):            # upsert created a document
            return pair if return_document else None
        before, after = pair
        return after if return_document else before

    async def delete_one(self, flt):
        await asyncio.sleep(0)
        d = self._find_first(flt)
        if d is not None:
            self.docs.remove(d)
        return type("R", (), {"deleted_count": 1 if d is not None else 0})()

    # ── reads ──
    async def find_one(self, flt=None, projection=None, **_):
        await asyncio.sleep(0)
        d = self._find_first(flt or {})
        return copy.deepcopy(d) if d is not None else None

    def find(self, flt=None, projection=None):
        return FakeCursor([d for d in self.docs if _match(d, flt or {})])

    async def count_documents(self, flt=None):
        await asyncio.sleep(0)
        return sum(1 for d in self.docs if _match(d, flt or {}))


class FakeDatabase:
    """Doubles as the bot's `Database` manager and as the Motor database object."""

    def __init__(self):
        self._cols = {}
        self.rewards_ready = True
        self.supports_transactions = False

    def __getitem__(self, name):
        if name not in self._cols:
            self._cols[name] = FakeCollection(name)
        return self._cols[name]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    async def list_collection_names(self):
        return list(self._cols)

    async def create_collection(self, name):
        if name in self._cols:
            raise OperationFailure("collection already exists")
        self._cols[name] = FakeCollection(name)
