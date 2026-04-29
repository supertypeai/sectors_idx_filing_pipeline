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


class PomptCollections: 
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
