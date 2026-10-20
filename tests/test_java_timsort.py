"""``backlot.java_timsort`` against Java's own `ArrayList.sort`.

Each row of :data:`_JAVA` is what OpenJDK 26's sort did on 2026-10-16 with one of three comparators
over items `(value, id)` of one of two shapes (:func:`_items`): the SHA-256 (first 16 hex digits)
of every `compare(a, b)` call as `"idA,idB;"` in call order, of the ids it left in order (nothing
where it threw), and `V` where it threw `IllegalArgumentException` for a comparator that breaks its
contract. `rps` is not transitive (`value % 3` beats the next class round a cycle, by value within
a class), `coarse` ties a hundred values at a time.
"""

import hashlib

import pytest

from backlot import java_timsort

_JAVA = """
    total 0 1 mod e3b0c44298fc1c14 e3b0c44298fc1c14 -
    total 1 1 mod e3b0c44298fc1c14 0a5a2432405b31b9 -
    total 33 1 tail 5406a603725644a7 7b85af835d7b8131 -
    total 64 1 tail c8411f03ccf04e33 148aec5efd8a3e3a -
    total 100 1 tail 6e22fc0f09337ab3 fc60bc917036cf11 -
    total 80 1 lohi ca0985e61c9c93ee 9d0794f0ffca9ad3 -
    total 200 1 lohi 01df36fd9a7fc895 3661a33de914cd55 -
    total 80 1 hilo e9cbc83ea06785d5 d1a3d486775c2888 -
    total 200 1 hilo 3a2b47a6691d8e22 71d9c58cf79a437c -
    total 2 1 mod 35df8f7285481b9f e32ae59fc612d983 -
    total 2 2 mod 35df8f7285481b9f e32ae59fc612d983 -
    total 2 3 mod 35df8f7285481b9f e32ae59fc612d983 -
    total 31 1 mod 8efb09a161705842 01f2155fde8a7ac9 -
    total 31 2 mod 69b9a5bd10da0fd5 e95209b373889ccb -
    total 31 3 mod d5248d15257061dd 5b1def5f2c81d1ef -
    total 32 1 mod 0f624d06802e78e1 f4174cab76efbc39 -
    total 32 2 mod a26fdb49505697a0 b8a383635b30fd9e -
    total 32 3 mod eee9ec333241934e fc49e816d22769f0 -
    total 33 1 mod 8144d1fba28b47b1 52970c9a549ed74c -
    total 33 2 mod 1bfbb95875f99dea 4f7b43795c18e9e6 -
    total 33 3 mod 2f41e074fc914933 b9b06a0acec80923 -
    total 64 1 mod 0abc43cbb6985dfc 7b3c317b23e2d10e -
    total 64 2 mod 47d21e68a4f9c82f 75642e91bc9baf6f -
    total 64 3 mod 2420846ab47332ae 0fd9b180acb69a73 -
    total 65 1 mod 2a46bfe1e5d8c202 6b095d5677663a62 -
    total 65 2 mod 2fa6bf6fe91727d6 08e10cbd927eb5b9 -
    total 65 3 mod 4c049ac0916198fa 93218b78f840c222 -
    total 257 1 mod 9dbe2ceb0a624d96 56ea44218f580862 -
    total 257 2 mod fe96b2f1acf7cd26 060ad698ab532fa1 -
    total 257 3 mod a0a9baef0c67a616 f267fc1a1e007698 -
    total 2000 1 mod 353e84df283cf9f3 b1413dd77374f3bc -
    total 2000 2 mod bd9b3f5f8c32c97d e7de590bf63cf1a5 -
    total 2000 3 mod 702f1e395c0b3a79 d9eba7828d8a04bf -
    rps 2 1 mod 35df8f7285481b9f e32ae59fc612d983 -
    rps 2 2 mod 35df8f7285481b9f e32ae59fc612d983 -
    rps 2 3 mod 35df8f7285481b9f e32ae59fc612d983 -
    rps 31 1 mod f671998465b5a497 9b82eaef3ff9bba1 -
    rps 31 2 mod 77305de201793eec 2844859a184e6793 -
    rps 31 3 mod 24ada389b54b8c3d 4310605132fdc909 -
    rps 32 1 mod a415eeb2b4dab105 36556d16e1b71b42 -
    rps 32 2 mod e5297b203ec67770 e3b0c44298fc1c14 V
    rps 32 3 mod e2ee9b695cd8d94e e3b0c44298fc1c14 V
    rps 33 1 mod 266416014c201fe0 606e1428c5e2e802 -
    rps 33 2 mod 884c934c09b2986d e3b0c44298fc1c14 V
    rps 33 3 mod d7206d63fad66d16 e3b0c44298fc1c14 V
    rps 64 1 mod 46f11b353eeb0423 e3b0c44298fc1c14 V
    rps 64 2 mod e5297b203ec67770 e3b0c44298fc1c14 V
    rps 64 3 mod e2ee9b695cd8d94e e3b0c44298fc1c14 V
    rps 65 1 mod 760d67218c38a35d 9617edd848377402 -
    rps 65 2 mod 1e977cba1d9e53f2 e3b0c44298fc1c14 V
    rps 65 3 mod c32ed4c2c0cfa6bd e3b0c44298fc1c14 V
    rps 257 1 mod 3ea1be6a4374158d e3b0c44298fc1c14 V
    rps 257 2 mod 1e977cba1d9e53f2 e3b0c44298fc1c14 V
    rps 257 3 mod c32ed4c2c0cfa6bd e3b0c44298fc1c14 V
    rps 2000 1 mod 097a34535cca45c3 b73e79614cbf9cf9 -
    rps 2000 2 mod 1d99d35c94a2866a e3b0c44298fc1c14 V
    rps 2000 3 mod 5f45d34e9ab38c90 58433d021f5ee237 -
    coarse 2 1 mod 35df8f7285481b9f e32ae59fc612d983 -
    coarse 2 2 mod 35df8f7285481b9f e32ae59fc612d983 -
    coarse 2 3 mod 35df8f7285481b9f 4290a836daf3e3e6 -
    coarse 31 1 mod 198e197c02f652f1 70e550c69872cd58 -
    coarse 31 2 mod dc05111119c3d642 775bcef1adc2b4f1 -
    coarse 31 3 mod 7f8f7ce124c33fb5 d8835a666a1eb933 -
    coarse 32 1 mod f95ac3025674a715 265f419dec8f697b -
    coarse 32 2 mod f8f792e8934458c4 cf63767acc748b03 -
    coarse 32 3 mod 96e641a05382d54a a83d7e9e14b865d8 -
    coarse 33 1 mod 9d455ea0216bb285 a035bde31d0f7a19 -
    coarse 33 2 mod 8e09997012d3dafb b15da085da3ee52a -
    coarse 33 3 mod f6b1f502a3e95029 bcbf02d8fcb84387 -
    coarse 64 1 mod 010a875b2bf4975c ecd31991353d6bdf -
    coarse 64 2 mod e002732224d606ac 0cba048716d50c75 -
    coarse 64 3 mod 630463fce6a88e1d 167e358b4ee3b77e -
    coarse 65 1 mod e57da06778015bb8 889950d6ec9ef2b6 -
    coarse 65 2 mod df3614c1cd1ba625 4d016c79e7f7d3cd -
    coarse 65 3 mod 19b279d551d42943 ff10b1a7d7d0e7cf -
    coarse 257 1 mod efe2c3c1f6a660d8 f42909e4accf44ee -
    coarse 257 2 mod c48c7f483e15221b 58c8ed208ccdc0ee -
    coarse 257 3 mod 718603b3fa12d397 290058a1e0a4613a -
    coarse 2000 1 mod 284271fe1baac8bc 8d4c56c9a5519b06 -
    coarse 2000 2 mod 924f65cc8f03b55f b17d05b28b97c323 -
    coarse 2000 3 mod ecc3dd159fa44fc7 6e36ec7b28048015 -
    total 100 1 runs 7b379e91719f46a2 2813bbffa77d8b71 -
    total 100 2 runs 7b379e91719f46a2 2813bbffa77d8b71 -
    total 100 3 runs d23fe3bb19af41e4 8048624516f840b0 -
    total 100 4 runs 0b244342a2251fc7 8d346b3b69b72cbb -
    total 500 1 runs ecf8a247b0edaf87 172ce3caef9063a7 -
    total 500 2 runs 97df230f165cfd5f 13735f813e4cbf85 -
    total 500 3 runs 1ec1cf89fc094058 3e1145df79900def -
    total 500 4 runs ea3511db48cbe206 a759084d7a97c8e3 -
    total 1000 1 runs 5dc58096c9674119 64d5b39cdc4ee18a -
    total 1000 2 runs c45c84ae7590769f 57da41d3a6ad719b -
    total 1000 3 runs cb3018332612b2a2 e3bd0d8c87a74d58 -
    total 1000 4 runs 413dd18d04e012cf 84d7f8e4058a30df -
    total 3000 1 runs 66b5e16cf1321553 b92dc1c2a1ece63e -
    total 3000 2 runs 7a03c92494d8da37 73dfe303770c5b07 -
    total 3000 3 runs 986cb542a0a57c33 17df4c7ab6ccab98 -
    total 3000 4 runs 59b03dc34d90e27b fc2f73c569196698 -
    rps 100 1 runs 51bd4d528829818a ba462ab63a2c2438 -
    rps 100 2 runs 6612fd3864991461 5f1ec33962fa1d84 -
    rps 100 3 runs c29a626a2ad8e196 e3b0c44298fc1c14 V
    rps 100 4 runs db60e4958ea2117e e3b0c44298fc1c14 V
    rps 500 1 runs a4e669cbde0ac690 e3b0c44298fc1c14 V
    rps 500 2 runs 9b4f341011cecc84 e3b0c44298fc1c14 V
    rps 500 3 runs 9d8612cafe81d9c2 e3b0c44298fc1c14 V
    rps 500 4 runs 69ff8062c2fb1ca8 e3b0c44298fc1c14 V
    rps 1000 1 runs a4e669cbde0ac690 e3b0c44298fc1c14 V
    rps 1000 2 runs 9b4f341011cecc84 e3b0c44298fc1c14 V
    rps 1000 3 runs 36cf3cf81c0ab4b3 e3b0c44298fc1c14 V
    rps 1000 4 runs 69ff8062c2fb1ca8 e3b0c44298fc1c14 V
    rps 3000 1 runs e2fa15de7b6600cc e3b0c44298fc1c14 V
    rps 3000 2 runs e32e1edba178f290 e3b0c44298fc1c14 V
    rps 3000 3 runs 6107b4b5a46a8db9 e3b0c44298fc1c14 V
    rps 3000 4 runs b1c63ab199e606f6 e3b0c44298fc1c14 V
    coarse 100 1 runs 7b379e91719f46a2 2813bbffa77d8b71 -
    coarse 100 2 runs 7b379e91719f46a2 2813bbffa77d8b71 -
    coarse 100 3 runs d23fe3bb19af41e4 8048624516f840b0 -
    coarse 100 4 runs 0b244342a2251fc7 8d346b3b69b72cbb -
    coarse 500 1 runs 4c332ff55165b0d5 efb938a25ecdec7d -
    coarse 500 2 runs cde78e5fd0581552 07e73d0a0f33cf8d -
    coarse 500 3 runs f36baa6eef98f66c 7e93febc3b4d7e73 -
    coarse 500 4 runs 3d1808627e485def d1954560b79035ed -
    coarse 1000 1 runs 82a885c246d49235 66e8591025fe66f3 -
    coarse 1000 2 runs 82cb23f232d8ecc6 8d688b433e7d5f72 -
    coarse 1000 3 runs d3a31ca04d915477 ed01c1a23ff62cb3 -
    coarse 1000 4 runs 2341c9376fe6d11a 141ace4acf615b69 -
    coarse 3000 1 runs 1d9d196af1021d76 28b5f940bf9bb37d -
    coarse 3000 2 runs eb6c3a03c537722d e730b535d05de8c9 -
    coarse 3000 3 runs 8378aaab01a12811 5fa0c51ecb815de6 -
    coarse 3000 4 runs 44354310541e72c3 38426ae38784a68f -
"""


def _items(n: int, seed: int, shape: str) -> list[tuple[int, int]]:
    """`mod`: value `(id * 7919 + seed * 104729) % 1000`. `runs`: ascending runs whose lengths
    (1-200) and starting values (0-999) come from a 64-bit LCG seeded with ``seed``, each next value
    its run's last plus 0-2 from the same LCG, so the sort merges uneven runs and gallops. `tail`:
    ascending, the last item least. `lohi` and `hilo`: two ascending halves that a merge trims to
    one item on the left and on the right."""
    if shape == "mod":
        return [((i * 7919 + seed * 104729) % 1000, i) for i in range(n)]
    half = n // 2
    if shape == "tail":
        return [(i if i < n - 1 else -1, i) for i in range(n)]
    if shape == "lohi":
        return [
            (i if i < half - 1 else 10000 if i == half - 1 else 5000 + i - half, i)
            for i in range(n)
        ]
    if shape == "hilo":
        return [
            (5000 + i if i < half else 4000 if i == half else 20000 + i - half, i) for i in range(n)
        ]
    state = seed

    def draw(bound: int) -> int:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) % 2**64
        return (state >> 33) % bound

    items: list[tuple[int, int]] = []
    while len(items) < n:
        length, value = 1 + draw(200), draw(1000)
        for _ in range(length):
            if len(items) == n:
                break
            items.append((value, len(items)))
            value += draw(3)
    return items


def _compare(kind: str):
    def compare(a, b):
        log.append(f"{a[1]},{b[1]};")
        va, vb = a[0], b[0]
        if kind == "total":
            return (va > vb) - (va < vb)
        if kind == "coarse":
            return (va // 100 > vb // 100) - (va // 100 < vb // 100)
        ra, rb = va % 3, vb % 3
        if ra == rb:
            return (va > vb) - (va < vb)
        return 1 if (ra - rb + 3) % 3 == 1 else -1

    log: list[str] = []
    return compare, log


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@pytest.mark.parametrize("row", _JAVA.split("\n")[1:-1], ids=lambda row: " ".join(row.split()[:4]))
def test_java_list_sort_makes_javas_calls_and_leaves_javas_order(row):
    kind, n, seed, shape, calls, order, violated = row.split()
    compare, log = _compare(kind)
    try:
        ids = "".join(
            f"{item[1]},"
            for item in java_timsort.java_list_sort(_items(int(n), int(seed), shape), compare)
        )
        threw = "-"
    except java_timsort.ContractViolation:
        ids, threw = "", "V"
    assert (_sha("".join(log)), _sha(ids), threw) == (calls, order, violated)
