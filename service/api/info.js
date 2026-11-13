// GET /api/info?format=text
import { endpoint } from '../lib/http.js'
import { info } from '../lib/core.js'
import { infoText } from '../lib/format.js'
export default endpoint(info, infoText)
