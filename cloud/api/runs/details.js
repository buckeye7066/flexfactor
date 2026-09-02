import { endpoint } from "../../lib/http.js";
import { runArtifact } from "../../lib/service.js";

export default endpoint({ methods: ["GET"], authenticated: true, binary: true },
  async (request, token) => runArtifact(
    token, String(request.query?.repository || ""), request.query?.run_id));
