import axios from "axios";

const API_URL = "http://localhost:8000/configuraciones";

export const getConfigs = async () => {
  const res = await axios.get(API_URL);
  return res.data;
};

export const createConfig = async (data) => {
  const res = await axios.post(API_URL, data);
  return res.data;
};
