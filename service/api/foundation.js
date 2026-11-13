// GET /api/foundation?q=<EIN or name>&state=&pick=&format=text
import { endpoint } from '../lib/http.js'
import { foundation } from '../lib/core.js'
import { foundationText } from '../lib/format.js'
export default endpoint(foundation, foundationText)
