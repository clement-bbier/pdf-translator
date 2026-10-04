# Sources

All segments in `tm/seed_zh_en.jsonl` are short extracts reproduced verbatim from public bilingual announcements published by Hong Kong listed issuers on HKEXnews (https://www.hkexnews.hk). They are used here as a small translation memory (and, in a separate evaluation set, for translation-quality evaluation), with attribution to the issuer and the original document. Copyright in the documents remains with the respective issuers. Each record of the seed also carries its issuer, document title, date, URLs and pages.

Text was extracted with PyMuPDF; the only changes are whitespace normalisation (hard line breaks joined, runs of spaces collapsed, spaces adjacent to CJK characters removed in the Chinese text). Note: on HKEXnews the Chinese PDF of these documents carries the next document number plus a `_c` suffix (e.g. `2026031800388.pdf` / `2026031800389_c.pdf`), not the same number.

## Glossary: `glossary/finance_zh.csv`

The file holds 59 financial terms. The source terms are Traditional Chinese as used in Hong Kong filings. The English targets are the renderings used in the companies' official English versions of their filings. The French targets are the project's own standard renderings.

## Documents used in the evaluation set

The evaluation set is not part of this repository tree. Its segments come from the four documents below, and it is kept disjoint from the translation memory.

### Tencent Holdings Limited (0700) - Announcement of the Annual Results for the Year Ended 31 December 2025
- Date: 2026-03-18
- English: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0318/2026031800388.pdf
- Chinese: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0318/2026031800389_c.pdf
- Segments taken: 10

### AIA Group Limited (1299) - Annual Results for the Year Ended 31 December 2025
- Date: 2026-03-19
- English: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0319/2026031900001.pdf
- Chinese: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0319/2026031900002_c.pdf
- Segments taken: 8

### Link Real Estate Investment Trust (0823) - Final Results Announcement for the Year Ended 31 March 2026
- Date: 2026-05-28
- English: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0528/2026052800355.pdf
- Chinese: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0528/2026052800356_c.pdf
- Segments taken: 7

### MTR Corporation Limited (0066) - Announcement of Audited Results for the Year Ended 31 December 2025
- Date: 2026-03-12
- English: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0312/2026031200332.pdf
- Chinese: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0312/2026031200333_c.pdf
- Segments taken: 5

## Documents used in `tm/seed_zh_en.jsonl`

### CK Hutchison Holdings Limited (0001), joint announcement with CK Infrastructure Holdings Limited (1038) - Connected Transaction and Discloseable Transaction - Joint Announcement Relating to Disposal of UK Power Networks
- Date: 2026-02-26
- English: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0226/2026022600005.pdf
- Chinese: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0226/2026022600006_c.pdf
- Segments taken: 9

### HSBC Holdings plc (0005) - Interim Results for 2026 and Consideration of Payment of Second Interim Dividend for 2026
- Date: 2026-07-23
- English: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0723/2026072300654.pdf
- Chinese: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0723/2026072300655_c.pdf
- Segments taken: 2

### MTR Corporation Limited (0066) - Tuen Mun A16 Station Package Two Property Development - Discloseable Transaction and Connected Transaction
- Date: 2026-09-15
- English: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0915/2026091501023.pdf
- Chinese: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0915/2026091501024_c.pdf
- Segments taken: 5

### Hong Kong Exchanges and Clearing Limited (0388) - 2025 Final Results, Dividend and Closure of Register of Members
- Date: 2026-02-26
- English: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0226/2026022600166.pdf
- Chinese: https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0226/2026022600167_c.pdf
- Segments taken: 16

No document is shared between the evaluation set and the translation memory.
