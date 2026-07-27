GET_DEAL_SCHEDULE_QUERY = """
    select SaleDealProduct from DailyTradeSheet  group by SaleDealProduct
    EXEC usp_WT_DealTagInfo_Amazon_UCase
        @SDate                              = ?,
        @EDate                              = ?,
        @TimeZone                           = 'PPT',
        @Product                            = ?,        -- Deal's Product, multi-select ie: 'ACS Energy,E,Carbon Free Energy,Bundled Energy and REC Deals,NWS'
        @Counterparty                       = ?,         -- Deal's Counterparty, multi-select ie: 'MRTU,City of San Jose'
        @Contract                           = 'ALL',     -- Deal's Contract, multi-select
        @TransType                          = ?,         -- Deal's Transaction Type. Options: ALL, P, S. P: Purchase, S: Sale
        @Market                             = 'ALL',     -- Deal's Market, multi-select ie: 'MISO,PJM,ISONE'
        @Zone                               = 'ALL',     -- Deal's Zone, multi-select ie: 'PSEG,MID-C'
        @OriginalMW                         = 0,         -- Include Deal original profile or not: options 0,1 - default 0
        @Tags                               = 'ALL',      -- Tag options: ALL,Available,UnAssignedTags. 'ALL' surfaces every tag + the IsScheduledTag flag
        @Source                             = 'ALL',     -- Tag's Source, multi-select
        @Sink                               = 'UECA_NLSL',  -- Tag's Sink, multi-select ie: 'UECA_NLSL'
        @MarketPathPSE                      = 'ALL',     -- Tag's Market Path PSE
        @PSEComment                         = '',        -- Tag's PSE comment, wild card search
        @userName                           = 'TanD',    -- user existing in system to run this
        @TDate                              = ?,
        @TEndDate                           = ?,
        @IgnoreTraderDate                   = ?,
        @applySinkToDeals                   = ?
"""

# Same procedure as GET_DEAL_SCHEDULE_QUERY, but surfaces the ALREADY-scheduled
# purchase AND sale deals (existing tags): @ShowScheduledTags = 1. Kept as a
# separate constant so the existing purchase/sale calls stay untouched and never
# pass @ShowScheduledTags (proc default 0). Only get_scheduled_purchase_sale_deals
# uses this query.
GET_DEAL_SCHEDULE_SCHEDULED_QUERY = """
    EXEC usp_WT_DealTagInfo_Amazon_UCase
        @SDate                              = ?,
        @EDate                              = ?,
        @TimeZone                           = 'PPT',
        @Product                            = ?,        -- Deal's Product, multi-select ie: 'ACS Energy,E,Carbon Free Energy,Bundled Energy and REC Deals,NWS'
        @Counterparty                       = ?,         -- Deal's Counterparty, multi-select ie: 'MRTU,City of San Jose'
        @Contract                           = 'ALL',     -- Deal's Contract, multi-select
        @TransType                          = ?,         -- Deal's Transaction Type. Options: ALL, P, S. P: Purchase, S: Sale
        @Market                             = 'ALL',     -- Deal's Market, multi-select ie: 'MISO,PJM,ISONE'
        @Zone                               = 'ALL',     -- Deal's Zone, multi-select ie: 'PSEG,MID-C'
        @Book                               = 'ALL',     -- Deal's Book, multi-select - low level book(s)
        @OriginalMW                         = 0,         -- Include Deal original profile or not: options 0,1 - default 0
        @Tags                               = 'ALL',      -- ALL required so @ShowScheduledTags=1 surfaces the already-scheduled deals+tags
        @Source                             = 'ALL',     -- Tag's Source, multi-select
        @Sink                               = 'UECA_NLSL',  -- Tag's Sink, multi-select ie: 'UECA_NLSL'
        @CPSE                               = 'ALL',     -- Tag's CPSE - multi-select ie: 'BRTM01'
        @MarketPathPSE                      = 'ALL',     -- Tag's Market Path PSE
        @PSEComment                         = '',        -- Tag's PSE comment, wild card search
        @userName                           = 'TanD',    -- user existing in system to run this
        @TDate                              = ?,
        @TEndDate                           = ?,
        @IgnoreTraderDate                   = ?,
        @applySinkToDeals                   = ?,
        @ShowScheduledTags                  = ?          -- 1: include already-scheduled tags (proc default 0)
"""