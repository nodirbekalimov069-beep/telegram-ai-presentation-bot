"use strict";

function imageInspectionDisabled() {
  throw new TypeError(
    "Image dimension inspection is disabled; the presentation engine requires explicit image sizing",
  );
}

module.exports = imageInspectionDisabled;
module.exports.default = imageInspectionDisabled;
module.exports.imageSize = imageInspectionDisabled;
module.exports.disableFS = function disableFS() {};
module.exports.disableTypes = function disableTypes() {};
module.exports.setConcurrency = function setConcurrency() {};
module.exports.types = [];
