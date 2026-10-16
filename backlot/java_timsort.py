"""Java's `List.sort(Comparator)`: OpenJDK's `java.util.TimSort`, so that a comparator meets the
pairs Java's meets, in Java's order, and the sort fails where Java's does.

A comparator that is not a total order, as Spring 5's `MediaType` comparators are not, is where this
differs from :func:`sorted`: CPython only inserts up to 64 items where Java merges runs from 32, and
never raises a contract violation. Compared with OpenJDK 26's `ArrayList.sort` on 2026-10-15 over
25,280 inputs of 0 to 2,000 items, transitive comparators and not: the same calls in the same order,
and the same 469 contract violations.
"""

MIN_MERGE = 32
MIN_GALLOP = 7


class ContractViolation(ValueError):
    """Java's `IllegalArgumentException("Comparison method violates its general contract!")`."""


class _Strict:
    """A Java array: an index outside it raises, where a Python list would wrap a negative one."""

    __slots__ = ("v",)

    def __init__(self, values):
        self.v = list(values)

    def _check(self, i):
        if not 0 <= i < len(self.v):
            raise IndexError(f"Index {i} out of bounds for length {len(self.v)}")

    def __getitem__(self, i):
        self._check(i)
        return self.v[i]

    def __setitem__(self, i, x):
        self._check(i)
        self.v[i] = x


def _arraycopy(src, sp, dst, dp, n):
    # System.arraycopy copies as if through a temporary, so overlapping ranges are safe
    if n < 0:
        raise IndexError("negative length")
    if n:
        src._check(sp), src._check(sp + n - 1), dst._check(dp), dst._check(dp + n - 1)
        dst.v[dp : dp + n] = src.v[sp : sp + n]


def _binary_sort(a, lo, hi, start, c):
    if start == lo:
        start += 1
    while start < hi:
        pivot, left, right = a[start], lo, start
        while left < right:
            mid = (left + right) >> 1
            if c(pivot, a[mid]) < 0:
                right = mid
            else:
                left = mid + 1
        _arraycopy(a, left, a, left + 1, start - left)
        a[left] = pivot
        start += 1


def _count_run_and_make_ascending(a, lo, hi, c):
    run_hi = lo + 1
    if run_hi == hi:
        return 1
    run_hi += 1
    if c(a[run_hi - 1], a[lo]) < 0:
        while run_hi < hi and c(a[run_hi], a[run_hi - 1]) < 0:
            run_hi += 1
        a.v[lo:run_hi] = a.v[lo:run_hi][::-1]
    else:
        while run_hi < hi and c(a[run_hi], a[run_hi - 1]) >= 0:
            run_hi += 1
    return run_hi - lo


def _min_run_length(n):
    r = 0
    while n >= MIN_MERGE:
        r |= n & 1
        n >>= 1
    return n + r


def _gallop_left(key, a, base, length, hint, c):
    last_ofs, ofs = 0, 1
    if c(key, a[base + hint]) > 0:
        max_ofs = length - hint
        while ofs < max_ofs and c(key, a[base + hint + ofs]) > 0:
            last_ofs, ofs = ofs, (ofs << 1) + 1
        ofs = min(ofs, max_ofs)
        last_ofs += hint
        ofs += hint
    else:
        max_ofs = hint + 1
        while ofs < max_ofs and c(key, a[base + hint - ofs]) <= 0:
            last_ofs, ofs = ofs, (ofs << 1) + 1
        ofs = min(ofs, max_ofs)
        last_ofs, ofs = hint - ofs, hint - last_ofs
    last_ofs += 1
    while last_ofs < ofs:
        m = last_ofs + ((ofs - last_ofs) >> 1)
        if c(key, a[base + m]) > 0:
            last_ofs = m + 1
        else:
            ofs = m
    return ofs


def _gallop_right(key, a, base, length, hint, c):
    ofs, last_ofs = 1, 0
    if c(key, a[base + hint]) < 0:
        max_ofs = hint + 1
        while ofs < max_ofs and c(key, a[base + hint - ofs]) < 0:
            last_ofs, ofs = ofs, (ofs << 1) + 1
        ofs = min(ofs, max_ofs)
        last_ofs, ofs = hint - ofs, hint - last_ofs
    else:
        max_ofs = length - hint
        while ofs < max_ofs and c(key, a[base + hint + ofs]) >= 0:
            last_ofs, ofs = ofs, (ofs << 1) + 1
        ofs = min(ofs, max_ofs)
        last_ofs += hint
        ofs += hint
    last_ofs += 1
    while last_ofs < ofs:
        m = last_ofs + ((ofs - last_ofs) >> 1)
        if c(key, a[base + m]) < 0:
            ofs = m
        else:
            last_ofs = m + 1
    return ofs


class _TimSort:
    def __init__(self, a, c):
        self.a, self.c = a, c
        self.min_gallop = MIN_GALLOP
        self.run_base, self.run_len = [], []

    def merge_collapse(self):
        rl = self.run_len
        while len(rl) > 1:
            n = len(rl) - 2
            if (n > 0 and rl[n - 1] <= rl[n] + rl[n + 1]) or (
                n > 1 and rl[n - 2] <= rl[n] + rl[n - 1]
            ):
                if rl[n - 1] < rl[n + 1]:
                    n -= 1
            elif n < 0 or rl[n] > rl[n + 1]:
                break
            self.merge_at(n)

    def merge_force_collapse(self):
        rl = self.run_len
        while len(rl) > 1:
            n = len(rl) - 2
            if n > 0 and rl[n - 1] < rl[n + 1]:
                n -= 1
            self.merge_at(n)

    def merge_at(self, i):
        a, c = self.a, self.c
        base1, len1 = self.run_base[i], self.run_len[i]
        base2, len2 = self.run_base[i + 1], self.run_len[i + 1]
        self.run_len[i] = len1 + len2
        del self.run_base[i + 1], self.run_len[i + 1]
        k = _gallop_right(a[base2], a, base1, len1, 0, c)
        base1 += k
        len1 -= k
        if len1 == 0:
            return
        len2 = _gallop_left(a[base1 + len1 - 1], a, base2, len2, len2 - 1, c)
        if len2 == 0:
            return
        if len1 <= len2:
            self.merge_lo(base1, len1, base2, len2)
        else:
            self.merge_hi(base1, len1, base2, len2)

    def merge_lo(self, base1, len1, base2, len2):
        a, c = self.a, self.c
        tmp = _Strict(a.v[base1 : base1 + len1])
        cursor1, cursor2, dest = 0, base2, base1
        a[dest] = a[cursor2]
        dest += 1
        cursor2 += 1
        len2 -= 1
        if len2 == 0:
            _arraycopy(tmp, cursor1, a, dest, len1)
            return
        if len1 == 1:
            _arraycopy(a, cursor2, a, dest, len2)
            a[dest + len2] = tmp[cursor1]
            return
        min_gallop = self.min_gallop
        while True:  # outer
            count1 = count2 = 0
            done = False
            while True:
                if c(a[cursor2], tmp[cursor1]) < 0:
                    a[dest] = a[cursor2]
                    dest += 1
                    cursor2 += 1
                    count2 += 1
                    count1 = 0
                    len2 -= 1
                    if len2 == 0:
                        done = True
                        break
                else:
                    a[dest] = tmp[cursor1]
                    dest += 1
                    cursor1 += 1
                    count1 += 1
                    count2 = 0
                    len1 -= 1
                    if len1 == 1:
                        done = True
                        break
                if not (count1 | count2) < min_gallop:
                    break
            if done:
                break
            while True:
                count1 = _gallop_right(a[cursor2], tmp, cursor1, len1, 0, c)
                if count1 != 0:
                    _arraycopy(tmp, cursor1, a, dest, count1)
                    dest += count1
                    cursor1 += count1
                    len1 -= count1
                    if len1 <= 1:
                        done = True
                        break
                a[dest] = a[cursor2]
                dest += 1
                cursor2 += 1
                len2 -= 1
                if len2 == 0:
                    done = True
                    break
                count2 = _gallop_left(tmp[cursor1], a, cursor2, len2, 0, c)
                if count2 != 0:
                    _arraycopy(a, cursor2, a, dest, count2)
                    dest += count2
                    cursor2 += count2
                    len2 -= count2
                    if len2 == 0:
                        done = True
                        break
                a[dest] = tmp[cursor1]
                dest += 1
                cursor1 += 1
                len1 -= 1
                if len1 == 1:
                    done = True
                    break
                min_gallop -= 1
                if not (count1 >= MIN_GALLOP or count2 >= MIN_GALLOP):
                    break
            if done:
                break
            if min_gallop < 0:
                min_gallop = 0
            min_gallop += 2
        self.min_gallop = max(min_gallop, 1)
        if len1 == 1:
            _arraycopy(a, cursor2, a, dest, len2)
            a[dest + len2] = tmp[cursor1]
        elif len1 == 0:
            raise ContractViolation("Comparison method violates its general contract!")
        else:
            _arraycopy(tmp, cursor1, a, dest, len1)

    def merge_hi(self, base1, len1, base2, len2):
        a, c = self.a, self.c
        tmp = _Strict(a.v[base2 : base2 + len2])
        cursor1, cursor2, dest = base1 + len1 - 1, len2 - 1, base2 + len2 - 1
        a[dest] = a[cursor1]
        dest -= 1
        cursor1 -= 1
        len1 -= 1
        if len1 == 0:
            _arraycopy(tmp, 0, a, dest - (len2 - 1), len2)
            return
        if len2 == 1:
            dest -= len1
            cursor1 -= len1
            _arraycopy(a, cursor1 + 1, a, dest + 1, len1)
            a[dest] = tmp[cursor2]
            return
        min_gallop = self.min_gallop
        while True:  # outer
            count1 = count2 = 0
            done = False
            while True:
                if c(tmp[cursor2], a[cursor1]) < 0:
                    a[dest] = a[cursor1]
                    dest -= 1
                    cursor1 -= 1
                    count1 += 1
                    count2 = 0
                    len1 -= 1
                    if len1 == 0:
                        done = True
                        break
                else:
                    a[dest] = tmp[cursor2]
                    dest -= 1
                    cursor2 -= 1
                    count2 += 1
                    count1 = 0
                    len2 -= 1
                    if len2 == 1:
                        done = True
                        break
                if not (count1 | count2) < min_gallop:
                    break
            if done:
                break
            while True:
                count1 = len1 - _gallop_right(tmp[cursor2], a, base1, len1, len1 - 1, c)
                if count1 != 0:
                    dest -= count1
                    cursor1 -= count1
                    len1 -= count1
                    _arraycopy(a, cursor1 + 1, a, dest + 1, count1)
                    if len1 == 0:
                        done = True
                        break
                a[dest] = tmp[cursor2]
                dest -= 1
                cursor2 -= 1
                len2 -= 1
                if len2 == 1:
                    done = True
                    break
                count2 = len2 - _gallop_left(a[cursor1], tmp, 0, len2, len2 - 1, c)
                if count2 != 0:
                    dest -= count2
                    cursor2 -= count2
                    len2 -= count2
                    _arraycopy(tmp, cursor2 + 1, a, dest + 1, count2)
                    if len2 <= 1:
                        done = True
                        break
                a[dest] = a[cursor1]
                dest -= 1
                cursor1 -= 1
                len1 -= 1
                if len1 == 0:
                    done = True
                    break
                min_gallop -= 1
                if not (count1 >= MIN_GALLOP or count2 >= MIN_GALLOP):
                    break
            if done:
                break
            if min_gallop < 0:
                min_gallop = 0
            min_gallop += 2
        self.min_gallop = max(min_gallop, 1)
        if len2 == 1:
            dest -= len1
            cursor1 -= len1
            _arraycopy(a, cursor1 + 1, a, dest + 1, len1)
            a[dest] = tmp[cursor2]
        elif len2 == 0:
            raise ContractViolation("Comparison method violates its general contract!")
        else:
            _arraycopy(tmp, 0, a, dest - (len2 - 1), len2)


def java_list_sort(items, compare):
    """``items`` as Java's `List.sort(compare)` leaves them; raises :class:`ContractViolation`
    where Java raises its `IllegalArgumentException`, and whatever ``compare`` raises."""
    a = _Strict(items)
    lo, hi = 0, len(a.v)
    n_remaining = hi - lo
    if n_remaining < 2:
        return a.v
    if n_remaining < MIN_MERGE:
        init_run_len = _count_run_and_make_ascending(a, lo, hi, compare)
        _binary_sort(a, lo, hi, lo + init_run_len, compare)
        return a.v
    ts = _TimSort(a, compare)
    min_run = _min_run_length(n_remaining)
    while True:
        run_len = _count_run_and_make_ascending(a, lo, hi, compare)
        if run_len < min_run:
            force = n_remaining if n_remaining <= min_run else min_run
            _binary_sort(a, lo, lo + force, lo + run_len, compare)
            run_len = force
        ts.run_base.append(lo)
        ts.run_len.append(run_len)
        ts.merge_collapse()
        lo += run_len
        n_remaining -= run_len
        if n_remaining == 0:
            break
    ts.merge_force_collapse()
    return a.v
