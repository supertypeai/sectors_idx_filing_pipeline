# IDX insider trading filings
## Document Format
- IDX format: Ownership Report or Any Changes in Ownership of Public Company Shares (Commonly attached directly at the main link)
    - Example: https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202510/04c7b018c1_f38fdcf2bd.pdf
- Non-IDX format: Share Ownership Report (Commonly attached in URL named "lamp1")
    - Example: https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202510/9aa675fd95_b80f8563b6.pdf

## Processing
### Details Required (sectors-kb/sectors_documentation/database_documentation.md) and Data Extration Methods
This table details the filings (insider trading) that made by each company in IDX (keterbukaan informasi)

**This table details the schema for insider trading filings extracted from official documents filed as IDX announcements.**
| Column Name                   | Data Type                   | Constraints                 | Description                                              | Notes / Method                                                                                              |
|------------------------------:|----------------------------:|:---------------------------:|:--------------------------------------------------------:|:-----------------------------------------------------------------------------------------------------------:|
| `id`                          | bigint                      | PRIMARY KEY                 | Unique identifier for each record.                       |                                                                                                            |
| `created_at`                  | timestamp with time zone    |                             | Timestamp when the record was created (auto-generated).  | when the record was added into the db                                                                      |
| `title`                       | text                        |                             | Title or headline related to the transaction or event.   | 'holder_name' 'transaction_type' of 'symbol.company_name' shares                                           |
| `body`                        | text                        |                             | Detailed description or content of the record.           | Generated from title + transaction details (change in shares, purpose, etc.).                              |
| `source`                      | text                        |                             | Filing or Transaction Documents URL.                     | Direct link to IDX document.                                                                                |
| `timestamp`                   | timestamp without time zone |                             | Date and time when the transaction/event occurred.       | Filing/transaction declaration date.                                                                        |
| `sector`                      | text                        |                             | The sector where the company operate at based on the IDX-IC |                                                                                                          |
| `sub_sector`                  | text                        |                             | The sub-sector where the company operate at based on the IDX-IC |                                                                                                      |
| `tags`                        | **ARRAY**                   |                             | List of keywords or labels for categorization.           | takeover = 'share_percentage_before' <50 and 'share_percentage_after >50' and vice versa.                  |
| `transaction_type`            | **ARRAY**                   |                             | Type of transaction                                      | buy, sell, others (award, transfer,etc)                                                                    |
| `holding_before`              | bigint                      |                             | Number of shares held before the transaction.            |                                                                                                            |
| `holding_after`               | bigint                      |                             | Number of shares held after the transaction.             |                                                                                                            |
| `amount_transaction`          | bigint                      |                             | Total number of shares involved in the transaction.      |                                                                                                            |
| `holder_type`                 | **ARRAY**                   |                             | Type of holder (insider/institution).                    | institution = securities/investment companies (trading in amount that has an impact)                       |
| `holder_name`                 | text                        |                             | Name of the person or entity holding the shares.         | TitleCase                                                                                                  |
| `price`                       | numeric                     |                             | Share price at the time of the transaction.              | Computed = 'transaction_value' / 'amount_transaction'                                                       |
| `transaction_value`           | numeric                     |                             | Total value of the transaction in monetary terms.        | Computed                                                                                                   |
| `price_transaction`           | jsonb                       |                             | JSON object with detailed price breakdown (if available).| JSON array: [{date, type, price, amount}, ...].                                                            |
| `share_percentage_before`     | double precision            |                             | Ownership percentage before the transaction.             |                                                                                                            |
| `share_percentage_after`      | double precision            |                             | Ownership percentage after the transaction.              |                                                                                                            |
| `share_percentage_transaction`| double precision            |                             | Ownership percentage change due to the transaction.      |                                                                                                            |
| `UID`                         | text                        |                             | Unique identifier string for reference.                  |                                                                                                            |
| `symbol`                      | text                        |                             | Stock symbol associated with the record.                 |                                                                                                            |

### Computation / Logic
**Mix of sell and buy transactions in a single filing**
* `transaction_value` are computed based on `price_transaction` details:
    - `transaction_value` = absolute value of [sum of (transaction price * transaction amount) of `transaction_type` sell] - [sum of (transaction price * transaction amount) of `transaction_type` buy]
    
    > Example: https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202507/c78b56f0a3_e4244aa2cb.pdf
   
    > **Buy Transactions:**
    > - 128 × 1,678,900 = 214,899,200
    > - 129 × 6,467,600 = 834,326,400
    > - 129 × 533,000 = 68,757,000
    > - Total buy shares = 1,678,900 + 6,467,600 + 533,000 = 8,679,500
    > - Total buy value = 214,899,200 + 834,326,400 + 68,757,000 = 1,117,982,600

    > **Sell Transactions:**
    > - 130 × 11,000,000 = 1,430,000,000
    > - 131 × 2,000,000 = 262,000,000
    > - 132 × 8,229,200 = 1,086,254,400
    > - 133 × 3,732,300 = 496,395,900
    > - 134 × 7,238,500 = 969,549,000
    > - 135 × 5,600,000 = 756,000,000
    > - 136 × 1,100,000 = 149,600,000
    > - Total sell shares = 38,899,000
    > - Total sell value = 5,149,799,300

    > **Net transaction value (Buy – Sell):**
    > - 1,117,982,600 - 5,149,799,300 = -4,031,816,700
    > - (negative indicates `transaction type` = ‘sell’)

    > **Net transacted share amount (Buy-Sell):**
    > - 8,679,500 - 38,899,000 = -30,219,500

    > **Weighted average price = net value/ net shares:**
    > - -4,031,816,700 / -30,219,500 = 133.418

    > **Final result:**
    > - `price`: 133.418
    > - `transaction_value`: 4,031,816,700
    > - `transaction_type`: sell

 - total buy share amount - total sell share amount = `amount_transaction` (if negative, `transaction type` = "sell"; if positive, `transaction type` = "buy")
 - total buy transaction value - total sell transaction value = `transaction_value` (if negative, `transaction type` = "sell"; if positive, `transaction type` = "buy")
 - the difference of transaction value / the difference of share amount = `price` (if negative, `transaction type` = "sell"; if positive, `transaction type` = "buy")

NOTE: record in absolute value

### Auto via Scraper & Manual via Streamlit (https://sectors-news.streamlit.app/insider_trading_pdf)
* Generate `UID` for the following scenarios:
    - Scenario 1: share transfer between two individual holders
        - share transfer between two parties (two different source links) = two filings
    - Scenario 2: share transfer between company treasury stock and holder_name
        - share award (no matching UID), hence = one filing
    - Scenario 3: share trade of a listed company on IDX
        - share purchase by listed company (only one source links) = two filings

* Criteria to fulfil to be pushed into the db:
    1. `share_percentage_transaction` > 0.5
    2. `transaction_value` > 100000000.

## Alert/Guardrails
1. `share_percentage_after` < `share_percentage_before` = `transaction type` = "sell" OR `share_percentage_after` > `share_percentage_before` = `transaction type` = "buy"; otherwise, flag for review.
2. `holding_after` < `holding_before` = `transaction type` = "sell" OR `holding_after` > `holding_before` = `transaction type` = "buy"; otherwise, flag for review.
3. `share_percentage_transaction` = absolute difference of share amount between `share_percentage_after` and `share_percentage_before`; otherwise, flag for review.
4. `amount_transaction` = absolute difference of holding amount between `holding_after` and `holding_before`; otherwise, flag for review.
5. `transaction_value` = absolute value of [sum of (transaction price * transaction amount) of `transaction_type` sell] - [sum of (transaction price * transaction amount) of `transaction_type` buy]; when `transaction_value` is positive, `transaction_type`= sell while when `transaction_value` is negative, `transaction_type`= buy; otherwise, flag for review.
6. when `symbol.company_name` = `holder_name` there should be another filing with matching `UID`; otherwise, flag for review.
7. when `price` is significantly different from the market price on the `timestamp` date, flag for review.
8. matching `source` without matching `UID`, flag for review.
* Ensure the above before conversion into news.

## Front-End Presentation
1. tradeShares (presented in table)
2. transferShares (presented in card)

- with matching `UID` of the same `symbol` = share transfer (Scenario 1)
    - `transaction_value` for both filings should be “0”.

- with no matching UID of the same symbol
    - When `holder_name` = individual name = share transfer (Scenario 2)
    - When `holder_name` = company name, check for matching `UID`.
        - TRUE (there’s filing with matching UID (within the +/- 10 filings) AND `symbol.company_name` = `holder_name`, it doesn’t appear (Scenario 3: trader)
        - TRUE (there’s filing with matching UID (within the +/- 10 filings) AND `symbol.company_name` DOES NOT EQUAL TO `holder_name`, it will be share trading (Scenario 3: stock being traded);
        - FALSE = shares transfer with company treasury stock (Scenario 2) AND price = "0" or NULL
            - `share_percentage_after` < `share_percentage_before`  = `holder_name` on card left while company treasury stock on card right
                - Signifying holder_name transferred shares back to the company treasury
            - `share_percentage_after` > `share_percentage_before`  = `holder_name` on card right while company treasury stock on card left
                - Signifying treasury stock is transferred to holder name

NOTE: Scenario 3 filings should have [intercorporate buy/sell] tag