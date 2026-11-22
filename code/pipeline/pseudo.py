"""Team stand-in function (council ruling D1, 2026-11-21): ONE GLOBAL KEYED LETTER MAP.

Drop-in for andre's code/pipeline/pseudo.py: same names and signatures, norm(s) and pseudo(surface, key=None).
  * a-z: one keyed single cycle over the 5 vowels and one over the 21 consonants, so vowel->vowel, consonant->consonant,
    no fixed points, a bijection; 0-9: one keyed cycle; case kept. Fullwidth Latin letters and digits follow the same map.
  * accented letters (any base + combining marks, e.g. e-acute, Cyrillic short i): the base walks its own cycle until
    base+marks composes to ONE code point with the same signature (UTF-8 and UTF-16 length, NFD length, membership of
    cp1252, cp1250, latin-1, cp1251, cp1253, cp932, gbk, big5, cp949). Cycle walking keeps it a bijection.
  * Cyrillic, Greek (vowel and consonant classes), Hiragana, Katakana, Hangul syllables, CJK ideographs: one keyed cycle
    inside each class of characters with the same signature (and, for kana, the same voicing-mark behaviour), so the
    byte length and the code pages a name was encodable in are kept. Voiced kana map through their base.
  * composed (NFC) and decomposed (NFD) spellings agree: each grapheme is mapped in NFC and returned in its input form.
  * substrings, prefixes, case variants, glued forms and Windows short names stay consistent with no alias table,
    because the map works per character: pseudo(a + b) == pseudo(a) + pseudo(b) for any split between graphemes.
Key (secret, never in the code folder): $DEID_PSEUDO_KEY, else the file named by $DEID_PSEUDO_KEY_FILE
(default ~/hack-osu/private/pseudo.key). With neither, a public DEV key is used and a warning is printed once: never
submit with it. The `key` argument: bytes override the key; a str is IGNORED, because engine.py passes the identity id
there (pseudo(surf, iid)) and the map must not depend on it.
CLI:  python pseudo.py selftest [identities.json]   python pseudo.py vectors > test_vectors.tsv
      python pseudo.py keysearch ids.json [more.json]   (reads the base secret from the key file, finds the first
      derived key with no stand-in equal to another original identity, writes it back to the key file; never prints it)
"""
import hashlib
import hmac
import os
import re
import sys
import unicodedata

DEV_KEY = b"dev-key-public-never-submit"
TEST_KEY = b"test-vector-key-not-secret"
_SIGENC = ("cp1252", "cp1250", "latin-1", "cp1251", "cp1253", "cp932", "gbk", "big5", "cp949")
_BLOCKS = ((0x00C0, 0x024F, "latx"), (0x0370, 0x03FF, "grk"), (0x0400, 0x04FF, "cyr"), (0x3041, 0x3096, "hira"), (0x30A1, 0x30FA, "kata"),
           (0x4E00, 0x9FFF, "cjk"), (0xAC00, 0xD7A3, "hangul"))
_VOWELS = {"grk": set("αεηιουω"), "cyr": set("аеиоуыэюя")}


def norm(s):
    """Unchanged from the teammate's version (identity keys are built with it): NFKD accent fold, lower, spaces."""
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return ' '.join(s.lower().split())


def _enc_ok(c, e):
    try:
        c.encode(e)
        return True
    except UnicodeError:
        return False


def _sig(c):
    return (len(c.encode("utf-8", "surrogatepass")), len(c.encode("utf-16-le", "surrogatepass")),
            len(unicodedata.normalize("NFD", c))) + tuple(_enc_ok(c, e) for e in _SIGENC)


def _block(c):
    o = ord(c)
    for lo, hi, name in _BLOCKS:
        if lo <= o <= hi:
            return name
    return None


class Map:
    def __init__(self, key: bytes):
        self.key = key
        self._cls = {}       # class key -> {char: next char}
        self._done = set()
        self._cache = {}
        v, c = self._cycle("aeiou", "v"), self._cycle("bcdfghjklmnpqrstvwxyz", "c")
        self.latin = {**v, **c}
        self.digit = self._cycle("0123456789", "d")

    def _h(self, tag, ch):
        return hmac.new(self.key, (tag + "\x1f" + ch).encode("utf-8", "surrogatepass"), hashlib.sha256).digest()

    def _cycle(self, chars, tag):
        order = sorted(chars, key=lambda ch: self._h(tag, ch))
        return {order[i]: order[(i + 1) % len(order)] for i in range(len(order))} if len(order) > 1 else {}

    def _class_key(self, c, b):
        k = (b, _sig(c), c in _VOWELS.get(b, ()))
        if b in ("hira", "kata"):
            k += tuple((m, _sig(unicodedata.normalize("NFC", c + m))) for m in ("\u3099", "\u309a")
                       if len(unicodedata.normalize("NFC", c + m)) == 1)
        if b in ("grk", "cyr", "latx"):
            u = c.upper()
            k += (c.islower(), c.isupper(), len(u) == 1 and u.lower() == c)
        return k

    def _class_cycle(self, c, b):
        if b not in self._done:                     # group the whole block once, one keyed cycle per class
            lo, hi = next((lo, hi) for lo, hi, n in _BLOCKS if n == b)
            groups = {}
            for o in range(lo, hi + 1):
                ch = chr(o)
                if unicodedata.category(ch)[0] != "L" or (b != "hangul" and len(unicodedata.normalize("NFD", ch)) != 1):
                    continue
                groups.setdefault(self._class_key(ch, b), []).append(ch)
            for k, mem in groups.items():
                self._cls[k] = self._cycle(mem, "k%r" % (k,))
            self._done.add(b)
        return self._cls.get(self._class_key(c, b), {})

    def _base_cycle(self, b):
        """(cycle, the base as it sits in the cycle, re-case function) for a base character, or None."""
        lo = b.lower()
        if len(lo) == 1 and lo in self.latin:
            return self.latin, lo, (str.upper if b.isupper() else str)
        blk = _block(b)
        if blk in ("grk", "cyr", "latx") and b.isupper() and len(lo) == 1 and lo.upper() == b:
            return self._class_cycle(lo, blk), lo, str.upper
        if blk and blk != "hangul":
            return self._class_cycle(b, blk), b, str
        return None

    def char(self, c):
        r = self._cache.get(c)
        if r is not None:
            return r
        r = c
        o = ord(c)
        if c in self.digit:
            r = self.digit[c]
        elif 0xFF10 <= o <= 0xFF19:
            r = chr(0xFF10 + int(self.digit[chr(o - 0xFF10 + 48)]))
        elif 0xFF21 <= o <= 0xFF3A or 0xFF41 <= o <= 0xFF5A:
            a = chr(o - 0xFEE0)
            r = chr(ord(self.latin[a.lower()].upper() if a.isupper() else self.latin[a]) + 0xFEE0)
        elif _block(c) == "hangul":
            r = self._class_cycle(c, "hangul").get(c, c)
        else:
            d = unicodedata.normalize("NFD", c)
            bc = self._base_cycle(d[0])
            if bc:
                cyc, x, rc = bc
                if len(d) == 1:
                    r = rc(cyc.get(x, x))
                else:                       # precomposed: walk the base's cycle until it composes with same signature
                    sig, marks = _sig(c), d[1:]
                    for _ in range(len(cyc)):
                        x = cyc.get(x, x)
                        t = unicodedata.normalize("NFC", rc(x) + marks)
                        if len(t) == 1 and _sig(t) == sig:
                            r = t
                            break
        if len(r) != 1 or len(r.encode("utf-8", "surrogatepass")) != len(c.encode("utf-8", "surrogatepass")):
            r = c
        self._cache[c] = r
        return r

    def __call__(self, s):
        out = []
        i, n = 0, len(s)
        while i < n:
            j = i + 1
            if 0x1100 <= ord(s[i]) <= 0x115F:          # decomposed Hangul syllable: L + V (+ T) jamo
                while j < n and 0x1160 <= ord(s[j]) <= 0x11FF:
                    j += 1
            while j < n and unicodedata.combining(s[j]):
                j += 1
            g = s[i:j]
            if len(g) == 1:
                out.append(self.char(g))
            else:
                c = unicodedata.normalize("NFC", g)
                if len(c) == 1:
                    r = self.char(c)
                    rg = unicodedata.normalize("NFD", r) if g == unicodedata.normalize("NFD", g) else r
                    ok = len(rg) == len(g) and len(rg.encode("utf-8")) == len(g.encode("utf-8"))
                    out.append(rg if ok else g)
                else:                                   # no precomposed form: map the base, keep the marks
                    out.append(self.char(g[0]) + g[1:])
            i = j
        return "".join(out)


_MAP = None
_MAPS = {}


def _key():
    k = os.environ.get("DEID_PSEUDO_KEY")
    if k:
        return k.encode("utf-8")
    p = os.environ.get("DEID_PSEUDO_KEY_FILE") or os.path.expanduser("~/hack-osu/private/pseudo.key")
    if os.path.exists(p):
        with open(p, "rb") as f:
            return f.read().strip()
    sys.stderr.write("pseudo.py: WARNING no key (DEID_PSEUDO_KEY / DEID_PSEUDO_KEY_FILE): using the public DEV key\n")
    return DEV_KEY


def key_id():
    """Short public fingerprint of the active key (for logs and version strings; does not reveal the key)."""
    return hashlib.sha256(b"key-id|" + (_MAP or _get()).key).hexdigest()[:8]


def _get():
    global _MAP
    if _MAP is None:
        _MAP = Map(_key())
    return _MAP


_TLD = set("com org net edu gov mil int io ai co uk de fr it es nl be ch at se no dk fi pl cz sk hu ro pt ie gr ru ua cn "
           "jp kr in au nz ca us br mx ar cl za il tr tw hk sg my th vn id ph eu info biz dev app tech xyz me tv cc ws lt lv "
           "ee si hr rs bg is lu".split())
_GTLD = set((
    "aaa aarp abarth abb abbott abbvie abc able abogado abudhabi ac academy accenture accountant accountants aco "
    "actor ad ads adult ae aeg aero aetna af afl africa ag agakhan agency ai aig airbus airforce airtel akdn al "
    "alfaromeo alibaba alipay allfinanz allstate ally alsace alstom am amazon americanexpress americanfamily amex "
    "amfam amica amsterdam analytics android anquan anz ao aol apartments app apple aq aquarelle ar arab aramco "
    "archi army arpa art arte as asda asia associates at athleta attorney au auction audi audible audio auspost "
    "author auto autos avianca aw aws ax axa az azure ba baby baidu banamex bananarepublic band bank bar barcelona "
    "barclaycard barclays barefoot bargains baseball basketball bauhaus bayern bb bbc bbt bbva bcg bcn bd be beats "
    "beauty beer bentley berlin best bestbuy bet bf bg bh bharti bi bible bid bike bing bingo bio biz bj black "
    "blackfriday blockbuster blog bloomberg blue bm bms bmw bn bnpparibas bo boats boehringer bofa bom bond boo "
    "book booking bosch bostik boston bot boutique box br bradesco bridgestone broadway broker brother brussels bs "
    "bt build builders business buy buzz bv bw by bz bzh ca cab cafe cal call calvinklein cam camera camp canon "
    "capetown capital capitalone car caravan cards care career careers cars casa case cash casino cat catering "
    "catholic cba cbn cbre cbs cc cd center ceo cern cf cfa cfd cg ch chanel channel charity chase chat cheap "
    "chintai christmas chrome church ci cipriani circle cisco citadel citi citic city cityeats ck cl claims "
    "cleaning click clinic clinique clothing cloud club clubmed cm cn co coach codes coffee college cologne com "
    "comcast commbank community company compare computer comsec condos construction consulting contact contractors "
    "cooking cookingchannel cool coop corsica country coupon coupons courses cpa cr credit creditcard creditunion "
    "cricket crown crs cruise cruises cu cuisinella cv cw cx cy cymru cyou cz dabur dad dance data date dating "
    "datsun day dclk dds de deal dealer deals degree delivery dell deloitte delta democrat dental dentist desi "
    "design dev dhl diamonds diet digital direct directory discount discover dish diy dj dk dm dnp do docs doctor "
    "dog domains dot download drive dtv dubai dunlop dupont durban dvag dvr dz earth eat ec eco edeka edu education "
    "ee eg email emerck energy engineer engineering enterprises epson equipment er ericsson erni es esq estate et "
    "etisalat eu eurovision eus events exchange expert exposed express extraspace fage fail fairwinds faith family "
    "fan fans farm farmers fashion fast fedex feedback ferrari ferrero fi fiat fidelity fido film final finance "
    "financial fire firestone firmdale fish fishing fit fitness fj fk flickr flights flir florist flowers fly fm fo "
    "foo food foodnetwork football ford forex forsale forum foundation fox fr free fresenius frl frogans frontdoor "
    "frontier ftr fujitsu fun fund furniture futbol fyi ga gal gallery gallo gallup game games gap garden gay gb "
    "gbiz gd gdn ge gea gent genting george gf gg ggee gh gi gift gifts gives giving gl glass gle global globo gm "
    "gmail gmbh gmo gmx gn godaddy gold goldpoint golf goo goodyear goog google gop got gov gp gq gr grainger "
    "graphics gratis green gripe grocery group gs gt gu guardian gucci guge guide guitars guru gw gy hair hamburg "
    "hangout haus hbo hdfc hdfcbank health healthcare help helsinki here hermes hgtv hiphop hisamitsu hitachi hiv "
    "hk hkt hm hn hockey holdings holiday homedepot homegoods homes homesense honda horse hospital host hosting hot "
    "hoteles hotels hotmail house how hr hsbc ht hu hughes hyatt hyundai ibm icbc ice icu id ie ieee ifm ikano il "
    "im imamat imdb immo immobilien in inc industries infiniti info ing ink institute insurance insure int "
    "international intuit investments io ipiranga iq ir irish is ismaili ist istanbul it itau itv jaguar java jcb "
    "je jeep jetzt jewelry jio jll jm jmp jnj jo jobs joburg jot joy jp jpmorgan jprs juegos juniper kaufen kddi ke "
    "kerryhotels kerrylogistics kerryproperties kfh kg kh ki kia kids kim kinder kindle kitchen kiwi km kn koeln "
    "komatsu kosher kp kpmg kpn kr krd kred kuokgroup kw ky kyoto kz la lacaixa lamborghini lamer lancaster lancia "
    "land landrover lanxess lasalle lat latino latrobe law lawyer lb lc lds lease leclerc lefrak legal lego lexus "
    "lgbt li lidl life lifeinsurance lifestyle lighting like lilly limited limo lincoln link lipsy live living lk "
    "llc llp loan loans locker locus lol london lotte lotto love lpl lplfinancial lr ls lt ltd ltda lu lundbeck "
    "luxe luxury lv ly ma madrid maif maison makeup man management mango map market marketing markets marriott "
    "marshalls maserati mattel mba mc mckinsey md me med media meet melbourne meme memorial men menu merckmsd mg mh "
    "miami microsoft mil mini mint mit mitsubishi mk ml mlb mls mm mma mn mo mobi mobile moda moe moi mom monash "
    "money monster mormon mortgage moscow moto motorcycles mov movie mp mq mr ms msd mt mtn mtr mu museum music "
    "mutual mv mw mx my mz na nab nagoya name natura navy nba nc ne nec net netbank netflix network neustar new "
    "news next nextdirect nexus nf nfl ng ngo nhk ni nico nike nikon ninja nissan nissay nl no nokia "
    "northwesternmutual norton now nowruz nowtv np nr nra nrw ntt nu nyc nz obi observer office okinawa olayan "
    "olayangroup oldnavy ollo om omega one ong onion onl online ooo open oracle orange org organic origins osaka "
    "otsuka ott ovh pa page panasonic paris pars partners parts party passagens pay pccw pe pet pf pfizer pg ph "
    "pharmacy phd philips phone photo photography photos physio pics pictet pictures pid pin ping pink pioneer "
    "pizza pk pl place play playstation plumbing plus pm pn pnc pohl poker politie porn post pr pramerica praxi "
    "press prime pro prod productions prof progressive promo properties property protection pru prudential ps pt "
    "pub pw pwc py qa qpon quebec quest racing radio re read realestate realtor realty recipes red redstone "
    "redumbrella rehab reise reisen reit reliance ren rent rentals repair report republican rest restaurant review "
    "reviews rexroth rich richardli ricoh ril rio rip ro rocher rocks rodeo rogers room rs rsvp ru rugby ruhr run "
    "rw rwe ryukyu sa saarland safe safety sakura sale salon samsclub samsung sandvik sandvikcoromant sanofi sap "
    "sarl sas save saxo sb sbi sbs sc sca scb schaeffler schmidt scholarships school schule schwarz science scot sd "
    "se search seat secure security seek select sener services seven sew sex sexy sfr sg sh shangrila sharp shaw "
    "shell shia shiksha shoes shop shopping shouji show showtime si silk sina singles site sj sk ski skin sky skype "
    "sl sling sm smart smile sn sncf so soccer social softbank software sohu solar solutions song sony soy spa "
    "space sport spot sr srl ss st stada staples star statebank statefarm stc stcgroup stockholm storage store "
    "stream studio study style su sucks supplies supply support surf surgery suzuki sv swatch swiss sx sy sydney "
    "systems sz tab taipei talk taobao target tatamotors tatar tattoo tax taxi tc tci td tdk team tech technology "
    "tel temasek tennis teva tf tg th thd theater theatre tiaa tickets tienda tiffany tips tires tirol tj tjmaxx "
    "tjx tk tkmaxx tl tm tmall tn to today tokyo tools top toray toshiba total tours town toyota toys tr trade "
    "trading training travel travelchannel travelers travelersinsurance trust trv tt tube tui tunes tushu tv tvs tw "
    "tz ua ubank ubs ug uk unicom university uno uol ups us uy uz va vacations vana vanguard vc ve vegas ventures "
    "verisign versicherung vet vg vi viajes video vig viking villas vin vip virgin visa vision viva vivo vlaanderen "
    "vn vodka volkswagen volvo vote voting voto voyage vu vuelos wales walmart walter wang wanggou watch watches "
    "weather weatherchannel webcam weber website wedding weibo weir wf whoswho wien wiki williamhill win windows "
    "wine winners wme wolterskluwer woodside work works world wow ws wtc wtf xbox xerox xfinity xihuan xin xxx xyz "
    "yachts yahoo yamaxun yandex ye yodobashi yoga yokohama you youtube yt yun za zappos zara zero zip zm zone "
    "zuerich zw "
    "local localhost lan internal").split())   # top-level labels of the Public Suffix List (MPL-2.0), ASCII
_SLD = set("ac co com edu gov net org or ne go".split())
_LABEL = re.compile(r"[A-Za-z0-9-]+\Z")


def _split_domain(s):
    """(local@, 'www.', body, '.tld') for a bare host name or e-mail address, else None. www. and the public suffix
    identify nobody and are kept, so the result still reads as a domain; the body is mapped like any other text."""
    if not s or "." not in s or any(c.isspace() for c in s):
        return None
    local, at, host = s.rpartition("@")
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.match(x) for x in labels):
        return None
    last = labels[-1].lower()
    if last not in _TLD:
        # other Public Suffix List endings (.berlin, .studio, .local): only for lower-case host names with no initial
        # first label, so a person written "j.doen" or "Example.Studio" is never read as a host
        if last not in _GTLD or s != s.lower() or len(labels[0].split("@")[-1]) < 2 and not at:
            return None
    k = 2 if len(labels) >= 3 and labels[-2].lower() in _SLD else 1
    body = labels[:-k]
    pre = ""
    if len(body) > 1 and body[0].lower() == "www":
        pre, body = body[0] + ".", body[1:]
    return local + at, pre, ".".join(body), "." + ".".join(labels[-k:])


def pseudo(surface, key=None):
    """Stand-in for a surface form: same length in code points, UTF-8 and UTF-16 bytes; same class per character.
    Host names and e-mail addresses keep a leading www. and their public suffix (.com, .ac.uk)."""
    if isinstance(key, (bytes, bytearray)):
        m = _MAPS.get(bytes(key)) or _MAPS.setdefault(bytes(key), Map(bytes(key)))
    else:
        m = _get()
    d = _split_domain(surface)
    if d:
        return m(d[0]) + d[1] + m(d[2]) + d[3]
    return m(surface)


# ----------------------------------------------------------------------------------------------- CLI and self test
_SAMPLES = ["Jorv Halbecks", "jhalbeck", "JHALBE~1", "KVOREN~1", "kvorenst", "MeshParts", "meshparts", "xBot", "XBOT",
            "QRT4DKappa", "Ilvés", "Ilve\u0301s", "Brönk", "Bro\u0308nk", "Dvůřák", "Pawińska", "Ulbés", "Tarne BrÃ¶nk",
            "WIDGETCO\\BFernholt (QWX40217)", "tolmara@exampleuni.edu", "www.fennwick-lab.de", "张 三", "李四", "山田 太郎",
            "テストモデル株式会社", "ガンダム", "カ\u3099ンダム", "Иванов", "Ёжиков", "Παπαδόπουλος", "홍길동", "ＡＢＣ１２３",
            "C:\\Users\\kvorenst\\Desktop", "0xQuillon", "3DP-FlexHinge-V2", "ALeX"]


def _vectors():
    return [(s, pseudo(s, TEST_KEY)) for s in _SAMPLES + ["Straße", "Ølvind Strøm", "Łucja Wróbel", "jhalbeck@exampleuni.edu", "www.example.ac.uk", "3dq.berlin", "q.wexler",
                                  "kvorenst.studio", "printbox.oncampus.local"]]


def selftest(ids_path=None):
    m = lambda x: pseudo(x, TEST_KEY)
    bad = []
    vec = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_vectors.tsv")
    if os.path.exists(vec):
        for line in open(vec, encoding="utf-8"):
            if line.startswith("#") or not line.strip():
                continue
            a, b = line.rstrip("\n").split("\t")
            a, b = a.encode().decode("unicode_escape"), b.encode().decode("unicode_escape")
            if m(a) != b:
                bad.append(("vector", a, m(a), b))
    vals = list(_SAMPLES)
    if ids_path:
        import json
        ids = json.load(open(ids_path, encoding="utf-8"))
        vals += sorted({v for k, r in ids.items() for v in [k] + list(r.get("forms", []))})
    for s in vals:
        t = m(s)
        if len(t) != len(s) or any(len(t.encode(e, "surrogatepass")) != len(s.encode(e, "surrogatepass")) for e in ("utf-8", "utf-16-le")):
            bad.append(("length", s, t))
        for e in ("cp1252", "cp1250", "cp932", "gbk"):
            if _enc_ok(s, e) and not _enc_ok(t, e):
                bad.append(("codepage " + e, s, t))
        if unicodedata.normalize("NFD", t) != m(unicodedata.normalize("NFD", s)) or unicodedata.normalize("NFC", t) != m(unicodedata.normalize("NFC", s)):
            bad.append(("nfc/nfd", s, t))
        if s.isascii() and m(s.upper()) != t.upper():
            bad.append(("case", s, t))
    fixed = [c for c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" if m(c) == c]
    if fixed:
        bad.append(("fixed points", "".join(fixed)))
    if ids_path:
        by = {}
        for k in ids:
            by.setdefault(norm(m(k)), set()).add(k)
        col = [s for s in by.values() if len(s) > 1]
        if col:
            bad.append(("collisions", len(col), sorted(col)[:3]))
    for b in bad[:20]:
        print("FAIL", *b)
    print("selftest: %d values, %d failures" % (len(vals), len(bad)))
    return not bad


def keysearch(ids_path, base, n=5000):
    import json
    ids = json.load(open(ids_path, encoding="utf-8"))
    keys = set(ids)

    def shape(x):   # a stand-in keeps the class of every character, so x can only map onto an id of the same shape
        return "".join("v" if c in "aeiou" else "c" if "a" <= c <= "z" else "d" if c.isdigit() else "L" if c.isalpha() else c
                       for c in x)
    by = {}
    for x in keys:
        by.setdefault(shape(x), []).append(x)
    cand = [x for v in by.values() if len(v) > 1 for x in v]
    best = None
    for i in range(n):
        k = hmac.new(base.encode(), b"key-search|%d" % i, hashlib.sha256).hexdigest().encode()
        m = lambda x, _k=k: pseudo(x, _k)
        eq = sum(1 for x in cand if norm(m(x)) in keys and norm(m(x)) != x)
        if best is None or eq < best[0]:
            best = (eq, i, k)
        if eq == 0:
            break
    print("key-search: counter %d, %d stand-ins equal another original identity (of %d)" % (best[1], best[0], len(keys)),
          file=sys.stderr)
    return best[2]


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "vectors":
        print("# input\toutput, under TEST_KEY (public), unicode_escape encoded")
        for s, t in _vectors():
            print(s.encode("unicode_escape").decode() + "\t" + t.encode("unicode_escape").decode())
    elif a and a[0] == "selftest":
        sys.exit(0 if selftest(a[1] if len(a) > 1 else None) else 1)
    elif a and a[0] == "keysearch":      # keysearch IDS.json [IDS2.json ...] : base secret from the key file, winner written back
        kf = os.environ.get("DEID_PSEUDO_KEY_FILE") or os.path.expanduser("~/hack-osu/private/pseudo.key")
        import json, tempfile
        ids = {}
        for p in a[1:]:
            ids.update(json.load(open(p, encoding="utf-8")))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as t:
            json.dump(ids, t)
        base = open(kf, "rb").read().strip().decode()
        k = keysearch(t.name, base, 5000)
        os.unlink(t.name)
        with open(kf + ".new", "wb") as f:
            f.write(k + b"\n")
        os.chmod(kf + ".new", 0o600)
        os.replace(kf + ".new", kf)
        print("key written to", kf, "key id", hashlib.sha256(b"key-id|" + k).hexdigest()[:8])
    else:
        print(__doc__)
