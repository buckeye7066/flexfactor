import { endpoint } from "../../lib/http.js";
import { runStatus } from "../../lib/service.js";

export default endpoint({ methods: ["GET"], authenticated: true }, async (request, token) => {
  return runStatus(token, String(request.query?.repository || ""), request.query?.run_id);
});
