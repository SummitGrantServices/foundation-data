// GET /api/funders-of?q=<recipient EIN or name>&state=&years=&limit=&format=text
import { endpoint } from '../lib/http.js'
import { fundersOf } from '../lib/core.js'
import { fundersOfText } from '../lib/format.js'
export default endpoint(fundersOf, fundersOfText)
