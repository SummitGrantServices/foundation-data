// GET /api/search?state=&county=&tags=&text=&years=&preselected=&min_grant=&limit=&format=text
import { endpoint } from '../lib/http.js'
import { search } from '../lib/core.js'
import { searchText } from '../lib/format.js'
export default endpoint(search, searchText)
