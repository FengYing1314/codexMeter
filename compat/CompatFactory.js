.pragma library

function component(parent, nativeType, legacyType) {
    // 0 为 Component.PreferSynchronous，模块均来自本地 DMS。
    var result = Qt.createComponent("qs.DCommon.Widgets", nativeType, 0, parent);
    if (result.status === 1) return result;

    var error = result.errorString();
    // 仅旧版缺少整个模块时回退，保留现代控件自身的真实错误。
    var missingModule = /module ["']qs\.DCommon\.Widgets["'] is not installed/i.test(error)
        || /no module named ["']qs\.DCommon\.Widgets["'] found/i.test(error);
    result.destroy();
    if (!missingModule) throw new Error(error);

    result = Qt.createComponent("qs.Widgets", legacyType, 0, parent);
    if (result.status !== 1) {
        error = result.errorString();
        result.destroy();
        throw new Error(error);
    }
    return result;
}
