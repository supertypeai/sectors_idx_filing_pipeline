from pydantic import Field, BaseModel


class PurposeTranslator(BaseModel):
    purpose: str = Field(
        description='Natural English translation of the Indonesian purpose text'
    )


class TitleBodyGeneration(BaseModel): 
    title: str = Field(
        description='News title for the filing transaction'
    )
    body: str = Field(
        description='One or two paragraph news body summarizing the filing with context'
    )


class PriceTransactionItem(BaseModel):
    type: str | None = Field(
        description=(
            "Normalized transaction type: 'buy', 'sell', or 'others'. "
            "Map the document's own wording (e.g. 'Pembelian' -> buy, 'Penjualan' -> sell, "
            "everything else -> others)."
        )
    )
    amount_transacted: int | None = Field(
        description="Number of shares transacted in this line item. Null if not stated."
    )
    price: float | None = Field(
        description=(
            "Price per share for this line item. Null if not stated "
            "(e.g. non-market transactions like inheritance or grants)."
        )
    )
    date: str | None = Field(
        description="Transaction date in YYYY-MM-DD format. Null if not stated."
    )
    purpose: str | None = Field(
        description="Stated purpose/reason for this transaction."
    )
    classification: str | None = Field(
        description=(
            "Share classification exactly as stated in the document "
            "(e.g. 'Saham Biasa'). Do not translate or normalize this value."
        )
    )


class FilingPayload(BaseModel):
    reasoning: str = Field(
        description=(
            "Explanation of how symbol, company_name, holder_name, holding_before/"
            "after, and share_percentage_before/after, price_transaction were identified in the document. "
            "If this document is not a share ownership report, or any field is not stated, "
            "say so explicitly here. Write this before deciding the field values below."
        )
    )
    company_name: str | None = Field(
        description=(
            "Full legal company name as stated in the document. Used as a fallback "
            "if the symbol cannot be matched to a known company. Null if not stated."
        )
    )
    holder_name: str | None = Field(
        description="Full name of the shareholder/insider making the report. Null if not stated."
    )
    holding_before: int | None = Field(
        description="Total shares held before this transaction. Null if not stated."
    )
    holding_after: int | None = Field(
        description="Total shares held after this transaction. Null if not stated."
    )
    share_percentage_before: float | None = Field(
        description="Ownership percentage before the transaction. Null if not stated."
    )
    share_percentage_after: float | None = Field(
        description="Ownership percentage after the transaction. Null if not stated."
    )
    price_transaction: list[PriceTransactionItem] = Field(
        default_factory=list,
        description=(
            "Every individual transaction line item found in the document's "
            "transaction table. Empty list if the document has no such table."
        )
    )


class PromptCollections: 
    @staticmethod
    def get_system_title_body_prompt():
        return """ 
            You are a financial news writer expert. covering the Indonesian stock market (IDX).
            Your job is to write a concise, factual news entry for a filing transaction.
            You will be given the current filing data and historical context of insider activity 
            at the same company over the last 6 months.
            Use the historical context to enrich the narrative where relevant, but do not speculate.
            Write in English. Be direct and specific. Do not use generic filler phrases.
        """
    
    @staticmethod
    def get_user_title_body_prompt():
        return """ 
            Write a professional financial news entry for the following insider filing transaction.

            Current filing:
            {current_filing}

            Historical insider activity context type: {context_type}
            Historical insider activity at the same company over the last 6 months:
            {context_transactions}

            Title format Use data from Current Filing:
            - transaction type is buy or sell:
                -(Holder name) (Transaction Type in Current Filling) Shares of (Company name)
            - transaction type is others: 
                -(Company name) Insider (Holder name) Reports Shareholding Change

            Body instructions:
            - Maximum One paragraph.
            - Written from the perspective of a financial journalist covering IDX insider transactions.
            - Lead with the most significant aspect of the transaction: size, price, ownership impact, or pattern.
            - If context_type is null or context_transactions is empty, focus solely on the current filing facts. Do not reference any historical pattern.
            - If the historical context reveals a meaningful pattern such as repeated accumulation, 
                coordinated insider buying, or broad portfolio repositioning, incorporate it naturally 
                into the narrative without using technical template labels like cluster, chain, or cross stock.
            - Quantify where possible: share count, transaction value, ownership percentage before and after, 
                average price per share. Do not enumerate individual transaction blocks
            - Currency: IDR. Comma as thousands separator. Dot for decimals.
            - If transaction type is others, identify and describe the specific corporate action 
                (e.g. share award, transfer, inheritance) rather than labeling it as others.
            - Purpose field may be in Indonesian, translate it naturally into English financial terminology. 
                Do not quote the Indonesian text directly.
            - Do not speculate. Do not editorialize. Do not use filler phrases like 
                "it is worth noting" or "this is significant because".

            Ensure return in the following JSON format.
            {format_instructions}
        """
    
    @staticmethod
    def get_system_purpose_prompt():
        return """
        You are a senior financial analyst fluent in both Indonesian and English corporate finance terminology.
        Your role is to translate Indonesian transaction purposes into precise, professional English 
        as they would appear in official regulatory filings or financial reports.
        Use correct financial and legal terminology where applicable.
        Do not translate word for word. Do not add explanation or commentary.
        Return only the translated text in the specified JSON format.
        """

    @staticmethod
    def get_user_purpose_prompt():
        return """
        Translate the following Indonesian transaction purpose into natural, professional English.

        Indonesian text:
        {purpose}

        {format_instructions}
        """

    @staticmethod
    def get_system_extraction_prompt():
        return """
            You are a financial data extraction expert covering Indonesian public company
            shareholder disclosures (share ownership / insider transaction reports).
            You will be given raw text extracted from a PDF whose layout is not standardized -
            it may be a full ownership report, a cover letter, a material-fact disclosure, or
            an unrelated attachment.

            Extract only what is explicitly and unambiguously stated in the text. Never infer,
            estimate, calculate, or guess a value that is not directly present. If a field is
            not stated, or you are not confident in it, return null for that field rather than
            filling it with a plausible-looking value.

            If the document does not contain a share ownership transaction table at all (for
            example, it is a cover letter or an unrelated disclosure), return null for every
            top-level field and an empty list for price_transaction. Do not force a match.

            Do not aggregate, sum, or summarize multiple transaction rows into one. Extract
            every individual row of the transaction table as a separate item, in the order it
            appears in the document.

            Every object you return (the top-level filing and each transaction row) has a
            'reasoning' field. Write that field first, citing where in the text each value
            came from, and only then decide the rest of the fields for that object.
        """

    @staticmethod
    def get_user_extraction_prompt():
        return """
            Extract the shareholder and transaction data from the following document text.

            Document text:
            {document_text}

            Field notes:
            - company_name: the full legal company name as stated in the document.
            - holder_name: the full name of the reporting shareholder/insider.
            - holding_before / holding_after: total shares held before and after this
                transaction, as stated in the document. Not the amount transacted.
            - share_percentage_before / share_percentage_after: ownership percentage before
                and after the transaction, as stated in the document.
            - price_transaction: one item per row of the transaction table.
                - type: normalize to 'buy', 'sell', or 'others' based on the document's own
                    wording (e.g. 'Pembelian' -> buy, 'Penjualan' -> sell).
                - amount_transacted: number of shares in that row.
                - price: price per share for that row. Null if the row has no price
                    (e.g. inheritance, grant, or other non-market transaction).
                - date: the row's transaction date, normalized to YYYY-MM-DD.
                - purpose: the stated purpose/reason for that row.
                - classification: the share classification exactly as written in the document
                    (e.g. 'Saham Biasa'). Do not translate or normalize this value.

            Ensure return in the following JSON format.
            {format_instructions}
        """
