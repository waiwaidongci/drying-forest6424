export const roles={admin:'负责人',analyst:'分析师',viewer:'业务阅读者'};
export const runStates={queued:'队列中',running:'正在计算',completed:'已完成',failed:'失败'};
export const reviewStates={draft:'待复核',approved:'已批准'};
export const deviceStates={pending:'待报齐',quarantined:'已隔离',effective:'已生效',conflict:'同刻异值'};
export const batchStates={writing:'写入未封',sealed:'已封窗'};
export const reasons={not_reported_through:'待报齐',same_instant_conflict:'同刻异值',no_prior_reading:'窗前无值',gap_exceeded:'断采超限',invalid_manual_values:'手录字段无效'};
